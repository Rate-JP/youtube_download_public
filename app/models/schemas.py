from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

TargetType = Literal["auto", "video", "playlist"]
DownloadTargetType = Literal["video", "playlist"]
MediaType = Literal["video", "audio"]
VideoQuality = Literal["2160p", "1440p", "1080p", "720p"]
AudioFormat = Literal["m4a", "mp3"]
FallbackPolicy = Literal["nearest_lower"]
PreferredContainer = Literal["mp4"]


class FormatsRequest(BaseModel):
    url: HttpUrl
    target_type: TargetType = "auto"
    playlist_start_index: int | None = Field(default=None, ge=1)
    playlist_end_index: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_playlist_range(self) -> "FormatsRequest":
        if (
            self.playlist_start_index is not None
            and self.playlist_end_index is not None
            and self.playlist_end_index < self.playlist_start_index
        ):
            raise ValueError("playlist_end_index must be greater than or equal to playlist_start_index")
        return self


class DownloadSourceItem(BaseModel):
    url: HttpUrl
    video_id: str | None = None
    playlist_id: str | None = None
    index: int | None = Field(default=None, ge=1)
    quality: VideoQuality | None = None
    audio_format: AudioFormat | None = None
    fallback_policy: FallbackPolicy | None = None
    preferred_container: PreferredContainer | None = None

    @field_validator("video_id", "playlist_id")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        return value.strip() if value else value


class DownloadItemRequest(BaseModel):
    url: HttpUrl
    target_type: DownloadTargetType
    media_type: MediaType
    video_id: str | None = None
    playlist_id: str | None = None
    index: int | None = Field(default=None, ge=1)
    quality: VideoQuality | None = None
    audio_format: AudioFormat | None = None
    fallback_policy: FallbackPolicy = "nearest_lower"
    preferred_container: PreferredContainer | None = None

    @field_validator("video_id", "playlist_id")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        return value.strip() if value else value

    @model_validator(mode="after")
    def validate_relations(self) -> "DownloadItemRequest":
        if self.target_type == "playlist":
            if not self.playlist_id:
                raise ValueError("playlist_id is required when target_type=playlist")
            if self.index is None:
                raise ValueError("index is required when target_type=playlist")

        if self.media_type == "video":
            if not self.quality:
                raise ValueError("quality is required when media_type=video")
            self.audio_format = self.audio_format or "m4a"
            if self.audio_format != "m4a":
                raise ValueError("audio_format must be m4a when media_type=video")
            self.preferred_container = self.preferred_container or "mp4"
        else:
            if not self.audio_format:
                raise ValueError("audio_format is required when media_type=audio")
            self.quality = None
            self.preferred_container = None

        return self


class DownloadRequest(BaseModel):
    items: list[DownloadSourceItem] = Field(min_length=1)
    target_type: DownloadTargetType
    media_type: MediaType
    quality: VideoQuality | None = None
    audio_format: AudioFormat | None = None
    fallback_policy: FallbackPolicy = "nearest_lower"
    preferred_container: PreferredContainer | None = None

    @model_validator(mode="after")
    def validate_request(self) -> "DownloadRequest":
        if self.media_type == "video":
            if not self.quality and any(item.quality is None for item in self.items):
                raise ValueError("quality is required when media_type=video (either request-level or per-item)")
            self.audio_format = self.audio_format or "m4a"
            if self.audio_format != "m4a":
                raise ValueError("audio_format must be m4a when media_type=video")
            self.preferred_container = self.preferred_container or "mp4"
        else:
            if not self.audio_format and any(item.audio_format is None for item in self.items):
                raise ValueError("audio_format is required when media_type=audio (either request-level or per-item)")
            self.quality = None
            self.preferred_container = None

        return self

    def to_item_requests(self) -> list[DownloadItemRequest]:
        return [
            DownloadItemRequest(
                url=item.url,
                video_id=item.video_id,
                playlist_id=item.playlist_id,
                index=item.index,
                target_type=self.target_type,
                media_type=self.media_type,
                quality=item.quality or self.quality,
                audio_format=item.audio_format or self.audio_format,
                fallback_policy=item.fallback_policy or self.fallback_policy,
                preferred_container=item.preferred_container or self.preferred_container,
            )
            for item in self.items
        ]


class ProgressBulkRequest(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=200)
