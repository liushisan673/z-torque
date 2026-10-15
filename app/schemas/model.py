from typing import Any, Literal
from pydantic import BaseModel, Field


class ModelProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    provider_type: str = Field(min_length=1, max_length=32)
    base_url: str | None = Field(default=None, max_length=512)
    secret_ref: str | None = None
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class ModelProviderPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    provider_type: str | None = Field(default=None, min_length=1, max_length=32)
    base_url: str | None = None
    secret_ref: str | None = None
    description: str | None = None
    config: dict[str, Any] | None = None


class ModelResourceCreate(BaseModel):
    provider_id: str
    name: str = Field(min_length=1, max_length=128)
    model_identifier: str = Field(min_length=1, max_length=128)
    interface_type: str | None = None
    supports_streaming: bool = True
    supports_function_calling: bool = True
    context_window: int | None = None
    max_output_tokens: int | None = None
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class ModelResourcePatch(BaseModel):
    provider_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=128)
    model_identifier: str | None = Field(default=None, min_length=1, max_length=128)
    interface_type: str | None = None
    supports_streaming: bool | None = None
    supports_function_calling: bool | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    description: str | None = None
    config: dict[str, Any] | None = None


class ModelEndpointCreate(BaseModel):
    provider_id: str
    model_id: str
    name: str = Field(min_length=1, max_length=128)
    endpoint_url: str = Field(min_length=1, max_length=512)
    priority: int = 0
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class ModelEndpointPatch(BaseModel):
    provider_id: str | None = None
    model_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=128)
    endpoint_url: str | None = Field(default=None, min_length=1, max_length=512)
    priority: int | None = None
    description: str | None = None
    config: dict[str, Any] | None = None
