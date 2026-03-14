from sqlalchemy import Column, Integer, String, Float, DateTime
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class User(Base):

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String)
    phone = Column(String, unique=True)
    password = Column(String)
    credits = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class CreditTransaction(Base):

    __tablename__ = "credit_transactions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer)
    amount = Column(Float)
    type = Column(String)
    method = Column(String)
    txn_id = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)