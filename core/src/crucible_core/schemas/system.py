from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    version: str
    api_version: str


class DatabaseStatus(BaseModel):
    path: str
    migration_revision: str
    journal_mode: str
    synchronous: str
    foreign_keys: bool


class OperationalStatusResponse(HealthResponse):
    address: str
    database: DatabaseStatus
