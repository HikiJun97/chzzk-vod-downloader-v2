import requests
import threading
import time as tm
import os
import subprocess
import shutil

from PySide6.QtCore import QThread, Signal
from concurrent.futures import ThreadPoolExecutor
from download.task import DownloadTask
from download.state import DownloadState

class DownloadThread(QThread):
    """
    VOD 파일을 multi-thread로 다운로드하는 작업 스레드 클래스
    """
    completed = Signal()
    stopped = Signal(str)

    def __init__(self, task: DownloadTask):
        super().__init__()
        self.task = task
        self.s = self.task.data
        self.future_dict = {}
        self.lock = self.task.lock
        self.logger = self.task.logger

    def run(self):
        """
        스레드가 시작될 때 자동으로 호출되는 메서드.
        실제 다운로드 파이프라인이 여기서 진행된다.
        """
        try:
            threading.current_thread().name = "DownloadThread"  # 스레드 시작 시 이름 재설정
            self.s.start_time = tm.time()
            total_size = self.s.total_size = self._get_total_size()

            # part_size 결정(해상도별 가중 적용)
            part_size = self._decide_part_size()

            # 다운로드할 구간 분할
            ranges = [
                (i * part_size, min((i + 1) * part_size - 1, total_size - 1))
                for i in range((total_size + part_size - 1) // part_size)
            ]
            self.s.max_threads = self.s.total_ranges = len(ranges)
            self.s.adjust_threads = min(self.s.adjust_threads, self.s.max_threads)
            self.s.threads_progress = [0] * self.s.total_ranges
            self.logger.log_download_start(total_size, part_size, self.s.total_ranges, self.s.adjust_threads)

            # 빈 파일 생성(사이즈: 0)
            with open(self.s.output_path, 'wb'):
                pass

            with ThreadPoolExecutor(max_workers=self.s.max_threads, thread_name_prefix="DownloadWorker") as executor:
                self.s.remaining_ranges = self._get_remaining_ranges(ranges)
                # 재사용 시 초기화 필수
                with self.lock:
                    self.s.future_count = 0
                    self.future_dict = {}

                while not self.task.state == DownloadState.WAITING:
                    # (1) 현재 활성 스레드 수보다 적으면 -> 추가 스레드 할당
                    while self.s.future_count < self.s.adjust_threads and self.s.remaining_ranges:
                        for part_num in range(self.s.adjust_threads):
                            if not self.s.remaining_ranges:
                                break
                            with self.lock:
                                if part_num not in self.future_dict:
                                    start, end = self.s.remaining_ranges.pop(0)
                                    self.s.future_count += 1
                                    self.logger.log_thread_start(part_num, start, end)
                                    future = executor.submit(
                                        self._download_part,
                                        start, end, part_num, total_size
                                    )
                                    future.add_done_callback(self._download_completed_callback)
                                    self.future_dict[part_num] = (start, end, future)

                    # (2) 주기적으로 상태 확인 (non-blocking)
                    tm.sleep(0.1)

                    # (3) 남은 작업이 없고 스레드도 없으면 종료
                    if not self.s.remaining_ranges and not self.future_dict:
                        break

                if self.task.state == DownloadState.RUNNING:
                    # MP4 파일 스트리밍 최적화 수행
                    self.task.item.post_process = True
                    if self._optimize_mp4_for_streaming():
                        self.logger.info("MP4 최적화 완료")
                    else:
                        self.logger.warning("MP4 최적화 실패, 원본 파일 유지")
                    self.task.item.post_process = False
                    
                    self.s.end_time = tm.time()
                    total_time = self.s.end_time - self.s.start_time
                    self.logger.log_download_complete(total_time)
                    self.logger.save_and_close()
                    self.completed.emit()

        except requests.RequestException as e:
            # 오류 발생 시 다운로드 파일 삭제
            if os.path.exists(self.s.output_path):
                os.remove(self.s.output_path)
            self.stopped.emit(self.tr("Download failed"))
            self.logger.log_exception("Download failed", e)
            self.logger.save_and_close()

        # 사용자가 강제로 중단한 경우 다운로드 파일 삭제
        if self.task.state == DownloadState.WAITING:
            if os.path.exists(self.s.output_path):
                os.remove(self.s.output_path)

    # ============ 다운로드 동작 관련 메서드들 ============

    def _download_part(self, start, end, part_num, total_size):
        """
        파일의 특정 구간(start~end)을 다운로드하는 함수.
        속도가 느릴 경우 재시도 로직, 일시정지/중지 핸들링을 포함한다.
        """
        slow_count = 0
        downloaded_size = 0
        while not self.task.state == DownloadState.WAITING:
            try:
                headers = {'Range': f'bytes={start}-{end}'}
                response = requests.get(self.s.base_url, headers=headers, stream=True, timeout=30)
                response.raise_for_status()
                part_start_time = tm.time()

                with open(self.s.output_path, 'r+b') as f:
                    f.seek(start)
                    for chunk in response.iter_content(chunk_size=8192):
                        if self.task.state == DownloadState.WAITING:
                            return part_num
                        if self.task.state == DownloadState.PAUSED:
                            self.s._pause_event.wait()

                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            elapsed = tm.time() - part_start_time

                            if elapsed > 0:
                                speed_kb_s = downloaded_size / elapsed / 1024
                                self._check_speed_and_update_progress(
                                    part_num, downloaded_size, total_size, speed_kb_s
                                )
                                if speed_kb_s < 100:
                                    slow_count += 1
                                    if slow_count > 5:
                                        # 속도가 너무 느리면 스레드 재시작
                                        with self.lock:
                                            self._download_stop_callback(start, end, part_num)
                                        return part_num
                                else:
                                    slow_count = 0

                            if downloaded_size >= (end - start + 1):
                                break

                # 성공적으로 마무리된 경우
                with self.lock:
                    self.s.completed_threads += 1
                    self.s.completed_progress += downloaded_size
                    self.s.threads_progress[part_num] = 0
                self.logger.log_thread_complete(part_num, downloaded_size)
                return part_num

            except (requests.RequestException, requests.Timeout) as e:
                with self.lock:
                    self._download_failed_callback(start, end, part_num)
                self.logger.log_error(f"Part {part_num} download failed", e)
                return part_num

    def _check_speed_and_update_progress(self, part_num, downloaded_size, total_size, speed_kb_s):
        """
        스레드가 다운로드 중일 때 속도 체크 및 진행 상황 업데이트.
        """
        with self.lock:
            self.s.threads_progress[part_num] = downloaded_size
            self.update_progress()

    # ============ 다운로드 조정 및 콜백 메서드 ============

    def _download_completed_callback(self, future):
        """
        특정 future(스레드)가 끝났을 때 호출되는 콜백.
        """
        try:
            part_num = future.result()
            # part_num 식별 후 future_dict에서 제거
            with self.lock:
                if part_num in self.future_dict:
                    del self.future_dict[part_num]
                    self.s.future_count -= 1
            self.update_progress()  # 즉각적 진행도 반영

        except Exception as e:
            # 일부 스레드가 오류로 중단된 경우
            self.stopped.emit(self.tr("Download failed"))
            self.logger.log_error("Thread failed", e)

    def _download_failed_callback(self, start, end, part_num):
        """
        예외 발생 시 파일 구간을 다시 다운로드할 수 있도록 remaining_ranges에 등록.
        """
        self.s.failed_threads += 1
        self.s.threads_progress[part_num] = 0
        self.s.remaining_ranges.append((start, end))

    def _download_stop_callback(self, start, end, part_num):
        """
        특정 스레드를 중도 중단하고, 해당 구간을 재시작하도록 설정.
        """
        self.s.restart_threads += 1
        self.s.threads_progress[part_num] = 0
        self.s.remaining_ranges.append((start, end))
        self.logger.warning(f"Part {part_num} stopped due to slow speed, will retry")

    # ============ 유틸 메서드 ============

    def _get_total_size(self): #: TODO: 컨텐츠 아이템에 content-length 추가(중복된 로직 제거)
        """
        HEAD 요청으로 total_size를 구한다.
        """
        response = requests.head(self.s.base_url)
        response.raise_for_status()
        size = int(response.headers.get('content-length', 0))
        if size == 0:
            resp = requests.get(self.s.base_url, stream=True)
            resp.raise_for_status()
            size = int(resp.headers.get('content-length', 0))
            resp.close()
        return size

    def _decide_part_size(self):
        """
        해상도에 따라 파트 크기 가중치를 달리 부여한다.
        """
        base_part_size = 1024 * 1024  # 1MB
        if self.s.content_type == 'clips':
            return base_part_size * 1
        elif self.s.resolution == 144:
            return base_part_size * 1
        elif self.s.resolution in [360, 480]:
            return base_part_size * 2
        elif self.s.resolution == 720:
            return base_part_size * 5
        else:
            return base_part_size * 10

    def _get_remaining_ranges(self, ranges):
        """
        중단 이후 재시작 같은 상황 고려(현재 파일크기 등을 바탕으로),
        아직 다운로드되지 않은 구간만 남겨 반환한다.
        """
        with open(self.s.output_path, 'r+b') as f:
            f.seek(0, 2)
            file_size = f.tell()

        remaining = []
        for start, end in ranges:
            if start >= file_size or end >= file_size:
                remaining.append((start, end))
        return remaining

    # ============ 진행 상황 업데이트 / 제어 메서드 ============

    def update_progress(self):
        """
        다운로드된 총량을 저장한다.
        """
        if self.task.state in [DownloadState.PAUSED, DownloadState.WAITING]:    # 중단 플래그 확인
            return

        active_downloaded_size = sum(self.s.threads_progress)
        self.s.total_downloaded_size = self.s.completed_progress + active_downloaded_size

    def _optimize_mp4_for_streaming(self):
        """
        MP4 파일을 스트리밍 재생에 최적화한다.
        FFmpeg의 faststart 옵션을 사용하여 메타데이터를 파일 앞으로 이동시킨다.
        
        Returns:
            bool: 최적화 성공 여부
        """
        if not self._is_ffmpeg_available():
            self.logger.warning("FFmpeg를 찾을 수 없습니다. MP4 최적화를 건너뜁니다.")
            return False
            
        temp_path = self.s.output_path + '.tmp'
        
        try:
            # FFmpeg 명령어 구성
            cmd = [
                'ffmpeg',
                '-i', self.s.output_path,      # 입력 파일
                '-c', 'copy',                  # 코덱 복사 (재인코딩 안함)
                '-movflags', 'faststart',      # 메타데이터를 파일 앞으로 이동
                '-y',                          # 덮어쓰기 확인 없음
                temp_path                      # 출력 파일
            ]
            
            # FFmpeg 실행
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                timeout=600  # 10분 타임아웃
            )
            
            if result.returncode == 0:
                # 최적화 성공: 임시 파일을 원본으로 교체
                if os.path.exists(temp_path):
                    # 원본 파일 크기와 임시 파일 크기 비교
                    orig_size = os.path.getsize(self.s.output_path)
                    temp_size = os.path.getsize(temp_path)
                    
                    # 파일 크기가 너무 다르면 최적화 실패로 간주
                    if abs(orig_size - temp_size) > orig_size * 0.1:  # 10% 이상 차이
                        self.logger.warning(f"최적화된 파일 크기가 비정상적입니다. 원본: {orig_size}, 최적화: {temp_size}")
                        os.remove(temp_path)
                        return False
                    
                    # 안전하게 파일 교체
                    shutil.move(temp_path, self.s.output_path)
                    self.logger.info(f"MP4 최적화 완료. 파일 크기: {temp_size} bytes")
                    return True
                else:
                    self.logger.error("최적화된 임시 파일을 찾을 수 없습니다.")
                    return False
            else:
                # FFmpeg 실행 실패
                error_msg = result.stderr.strip() if result.stderr else "알 수 없는 오류"
                self.logger.error(f"FFmpeg 최적화 실패: {error_msg}")
                
                # 임시 파일이 생성되었다면 삭제
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                return False
                
        except subprocess.TimeoutExpired:
            self.logger.error("FFmpeg 최적화가 타임아웃되었습니다.")
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return False
        except Exception as e:
            self.logger.error(f"MP4 최적화 중 예외 발생: {str(e)}")
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return False

    def _is_ffmpeg_available(self):
        """
        시스템에 FFmpeg가 설치되어 있는지 확인한다.
        
        Returns:
            bool: FFmpeg 사용 가능 여부
        """
        try:
            result = subprocess.run(
                ['ffmpeg', '-version'], 
                capture_output=True, 
                timeout=10
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
