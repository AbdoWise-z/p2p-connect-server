from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    redis_url:      str  = "redis://localhost:6379"
    known_pubkeys:  str  = ""   # comma-separated list of accepted public keys
                                 # leave empty to allow any key (dev mode)
    log_level:      str  = "info"

    @property
    def allowed_keys(self) -> set[str]:
        if not self.known_pubkeys.strip():
            return set()   # empty = open / dev mode
        return {k.strip() for k in self.known_pubkeys.split(",")}

    class Config:
        env_file = ".env"


settings = Settings()