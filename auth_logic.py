# auth_logic.py
from datetime import datetime, timedelta
from typing import Dict, Any

# Kriptografi için gerekli kütüphaneler
# pip install passlib python-jose
from passlib.context import CryptContext
from jose import JWT, jws, jwt

# Şifre hashleme ayarları (BCrypt, güncel ve güvenli bir standarttır)
PWD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT (JSON Web Token) Ayarları
# NOT: Gerçek uygulamada bu SADECE sunucu tarafında tutulur ve çok uzun olmalıdır!
SECRET_KEY = "YOUR-ULTRA-SECURE-SECRET-KEY-FOR-TRADEMIRROR-GLOBAL"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30  # Güvenlik için token'ın ömrü

# --- 1. Şifre Güvenliği Fonksiyonları ---

def hash_password(password: str) -> str:
    """Verilen şifreyi BCrypt ile hash'ler."""
    return PWD_CONTEXT.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Girilen şifreyi, kayıtlı hash ile karşılaştırır."""
    return PWD_CONTEXT.verify(plain_password, hashed_password)

# --- 2. JWT Token Fonksiyonları ---

def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """Kullanıcı verisiyle (ID) JWT token oluşturur."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        # Varsayılan süre
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """JWT token'ı çözer ve kullanıcı verisini döndürür."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except Exception:
        return None # Token geçersiz veya süresi dolmuş
