from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlmodel import create_engine


app = FastAPI(default_response_class=HTMLResponse)
engine = create_engine("sqlite:///dev.db")


@app.get("/")
async def index():
    return "hey there!"


