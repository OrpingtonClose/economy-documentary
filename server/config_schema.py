from pydantic import BaseModel, Field

class PipelineConfig(BaseModel):
    """Central configuration schema for the documentary pipeline.
    
    This is passed programmatically and loaded from config files, avoiding any environment variable usage.
    """
    capabilities: list[str] = Field(default_factory=list, description="Capabilities to load at startup")
    log_dir: str = Field(default="/tmp/documentary-pipeline", description="Directory for events database and logs")
    max_run_budget_usd: float = Field(default=20.0, description="Total budget ceiling for the pipeline run")

    model_config = {
        "extra": "ignore"
    }
