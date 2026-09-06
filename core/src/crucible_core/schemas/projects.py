from pydantic import BaseModel


class Project(BaseModel):
    id: str
    git_root: str
    max_snapshot_file_size_bytes: int = 1_048_576
