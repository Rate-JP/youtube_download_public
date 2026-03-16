from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

TargetType = Literal["auto", "video", "playlist"]
DownloadTargetType = Literal["video", "playlist_item"]
OutputFormat = Literal["mp4", "mp3", "m4a"]


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


class DownloadItemRequest(BaseModel):
    url: HttpUrl
    target_type: DownloadTargetType
    video_id: str | None = None
    playlist_id: str | None = None
    index: int | None = Field(default=None, ge=1)
    format: OutputFormat
    format_id: str | None = None

    @field_validator("video_id", "playlist_id", "format_id")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        return value.strip() if value else value

    @model_validator(mode="after")
    def validate_relations(self) -> "DownloadItemRequest":
        if self.format == "mp4" and not self.format_id:
            raise ValueError("format_id is required when format=mp4")
        if self.format in {"mp3", "m4a"}:
            self.format_id = None
        if self.target_type == "playlist_item":
            if not self.playlist_id:
                raise ValueError("playlist_id is required when target_type=playlist_item")
            if self.index is None:
                raise ValueError("index is required when target_type=playlist_item")
        return self


class DownloadRequest(BaseModel):
    items: list[DownloadItemRequest] = Field(min_length=1)


class ProgressBulkRequest(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=200)
