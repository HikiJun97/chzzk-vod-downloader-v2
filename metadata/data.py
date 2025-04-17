from PySide6.QtCore import QObject
from download.state import DownloadState

class MetadataItem(QObject):
    # 메타데이터 카드 리스트 아이템 데이터(DTO)

    def __init__(self, vod_url, metadata, unique_reps, resolution, base_url, download_path):
        self.vod_url = vod_url
        
        self.default_title = metadata.get('title', 'Unknown Title')
        self.title = self.default_title
        self.thumbnail_url = metadata.get('thumbnailImageUrl', '')
        self.category = metadata.get('videoCategoryValue', 'Unknown Category')
        self.channel_name = metadata.get('channelName', 'Unknown Channel')
        self.channel_image_url = metadata.get('channelImageUrl', '')
        self.live_open_date = metadata.get('liveOpenDate', 'Unknown Date')
        self.duration = metadata.get('duration', 0)

        self.unique_reps = unique_reps
        
        self.resolution = resolution
        self.total_size = ""
        self.base_url = base_url

        self.download_path = download_path
        self.output_path = ""

        self.download_size = ""
        self.download_progress = 0  # 다운로드 진행률 (0~100)
        self.download_speed = ""  # 다운로드 속도 (예: "2.5 MB/s")
        self.download_remain_time = ""  # 남은 다운로드 예상 시간 (예: "00:00:01")
        self.download_time = ""
        self.stateMessage = self.tr("Download waiting")

        self.downloadState = DownloadState.WAITING  # 초기 상태

    def setDownloadState(self, state: DownloadState, message: str = None):
        self.stateMessage = message
        if self.downloadState != state:
            self.downloadState = state