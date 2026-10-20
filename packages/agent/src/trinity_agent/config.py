from pydantic import Field
from pydantic_settings import BaseSettings


class AgentSettings(BaseSettings):
    model_config = {"env_file": ".env", "extra": "ignore"}

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    agent_model: str = Field(default="claude-sonnet-4-6", alias="AGENT_MODEL")
    max_iterations: int = Field(default=10, alias="AGENT_MAX_ITERATIONS")
    workspace_dir: str = Field(default="workspace", alias="AGENT_WORKSPACE_DIR")


settings = AgentSettings()
