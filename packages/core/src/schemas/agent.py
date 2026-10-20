from typing import Optional
from pydantic import BaseModel


class AgentRequest(BaseModel):
    content: str
    # The client's language; decides the answer's language when none is chosen in Settings (src/language.py).
    locale: Optional[str] = None
