import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Docker will inject this directly. 
# For local running, main.py will load the .env file.
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set!")

engine = create_engine(
    DATABASE_URL, 
    connect_args={"options": "-c statement_cache_capacity=0"} # Critical for Supabase
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()