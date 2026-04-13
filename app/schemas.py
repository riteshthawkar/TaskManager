from pydantic import BaseModel


class TaskCreate(BaseModel):
    title: str
    description: str = ""


class TaskConfirm(BaseModel):
    title: str
    description: str
    priority: int
    deadline: str  # ISO format string
    estimated_completion: str = ""
    llm_questions: str = ""
    llm_answers: str = ""


class LLMAnalysis(BaseModel):
    questions: list[str]
    suggested_priority: int
    suggested_deadline: str
    estimated_completion_date: str
    reasoning: str
