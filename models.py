from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class HWIDReset(db.Model):
    """Model for tracking HWID resets."""
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), index=True, nullable=False)
    reset_count = db.Column(db.Integer, default=0)
    is_invalid = db.Column(db.Boolean, default=False)
    last_reset = db.Column(db.DateTime, default=None)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f"<HWIDReset {self.key}>"