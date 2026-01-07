# app.py - TradeMirror Global Backend (PostgreSQL ve Admin123 Varsayılanları ile)

from fastapi import FastAPI, HTTPException, Depends, status, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import json
import random
import os

# --- GEREKLİ CELERY IMPORTLARI (Arka Plan Görevleri İçin) ---
try:
    # Bu importlar, Celery ve CCXT kütüphanelerinin kurulu olmasını gerektirir.
    from celery_worker import synchronize_user_trades_task, celery_app
except ImportError:
    synchronize_user_trades_task = None
    celery_app = None
    print("UYARI: Celery bileşenleri içeri aktarılamadı. Arka plan görevleri çalışmayacaktır.")


# --- E-POSTA İMPORTLARI ---
import smtplib
import ssl
from email.message import EmailMessage

# --- VERİTABANI İMPORTLARI ---
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, func
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.exc import OperationalError

# --- GÜVENLİK İMPORTLARI ---
from jose import jwt, JWTError
from passlib.context import CryptContext

# --- ORTAM DEĞİŞKENLERİ VE SABİTLER (Prodüksiyon Ayarları) ---

# Veritabanı URL'sini Ortam Değişkeninden al, yoksa yerel varsayılanı kullan (CRITICAL)
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:admin123@localhost:5432/borsa")
SECRET_KEY = os.environ.get("SECRET_KEY", "gizli-anahtariniz-burada-olmalidir-RENDER_ENV")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 1 Hafta

# E-posta Ayarları (Gelecekteki ölçekleme için)
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "trademirror_noreply@mail.com")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "varsayilan_email_sifresi")
EMAIL_SMTP_SERVER = os.environ.get("EMAIL_SMTP_SERVER", "smtp.gmail.com")
EMAIL_SMTP_PORT = int(os.environ.get("EMAIL_SMTP_PORT", 587))


# --- GÜVENLİK YAPILANDIRMALARI ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/token")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

# --- JWT OLUŞTURMA VE KOD ÇÖZME ---

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# --- VERİTABANI MODELİ VE BAĞLANTISI ---

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    user_id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    is_active = Column(bool, default=True)
    api_key_encrypted = Column(Text, nullable=True) # Şifrelenmiş olarak saklanacak
    api_secret_encrypted = Column(Text, nullable=True)
    setup_complete = Column(bool, default=False)

class Transaction(Base):
    __tablename__ = "transactions"
    transaction_id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer)
    trade_id = Column(String, index=True, nullable=True) # Borsa işlem ID'si veya manuel
    is_winning = Column(bool)
    pnl_pct = Column(Float) # Kâr/Zarar Yüzdesi
    max_drawdown_pct = Column(Float) # Pozisyon içindeki maksimum kayıp çekme
    duration_hours = Column(Float) # Pozisyonda kalma süresi (saat)
    volatility_pct = Column(Float) # İşlem sırasındaki ortalama oynaklık
    exit_time = Column(DateTime, default=datetime.utcnow)

class DNAMetric(Base):
    __tablename__ = "dna_metrics"
    metric_id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer)
    metric_name = Column(String, index=True)
    value = Column(Float)
    is_ideal = Column(bool, default=False) # İdeal profil mi yoksa gerçek mi?
    last_updated = Column(DateTime, default=datetime.utcnow)

class DNAProfile(Base):
    __tablename__ = "dna_profiles"
    profile_id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, unique=True)
    risk_tolerance = Column(String, default="Moderate")
    dominant_emotion = Column(String, default="Neutral")
    last_updated = Column(DateTime, default=datetime.utcnow)

# Engine, Session ve Veritabanı Bağlantısı
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- BAŞLANGIÇ İŞLEMLERİ ---

def create_initial_admin_user(db: Session):
    admin_user = db.query(User).filter(User.email == "admin@trademirror.com").first()
    
    # Mevcut kullanıcı varsa, yeni bir tane oluşturmayı veya şifrelemeyi atla. (GÜVENLİ MANTIK)
    if admin_user:
        print("INFO: Yönetici kullanıcı ('admin@trademirror.com') zaten mevcut.")
        return admin_user # 🚨 BU SATIR, ÜRETİM ORTAMINDA KODUN KALICI OLMASI GEREKEN HALİDİR.
    
    # Kullanıcı yoksa, varsayılan admini oluştur.
    hashed_password = pwd_context.hash("admin123")
    
    new_user = User(
        email="admin@trademirror.com",
        hashed_password=hashed_password,
        is_active=True,
        setup_complete=False # Kurulumu tamamlamaya zorlamak için False
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    print("INFO: Başlangıç yönetici kullanıcısı ('admin@trademirror.com') oluşturuldu.")
    return new_user

def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        create_initial_admin_user(db)
        db.close()
    except OperationalError as e:
        print(f"KRİTİK HATA: Veritabanı bağlantı hatası! Hata: {e}")
        print("Lütfen DATABASE_URL ortam değişkenini kontrol edin.")

# --- FASTAPI UYGULAMASI ---

app = FastAPI(
    title="TradeMirror Global API",
    description="Davranışsal Analiz Portalı Backend Hizmetleri",
    version="1.0.0",
    on_startup=[init_db] # Uygulama başlarken veritabanını oluştur
)

# CORS ayarları
origins = [
    "http://localhost",
    "http://localhost:8000",
    "http://localhost:8080",
    "https://borsa-xeqq.onrender.com", # Buraya Render URL'nizi ekleyin
    "https://*.ngrok-free.dev" # Ngrok test ortamı için
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Prodüksiyon ortamında bunu kısıtlamanız önerilir!
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Statik dosyaları (HTML, CSS, JS) sun
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- KULLANICI ŞEMALARI (Pydantic) ---

class UserCreate(BaseModel):
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

class APISetup(BaseModel):
    api_key: str
    api_secret: str

class TransactionCreate(BaseModel):
    trade_id: str
    is_winning: bool
    duration_hours: float
    pnl_pct: float
    max_drawdown_pct: float
    volatility_pct: float
    exit_time: datetime

# --- YARDIMCI GÜVENLİK FONKSİYONLARI ---

def get_user(db: Session, email: str):
    return db.query(User).filter(User.email == email).first()

def authenticate_user(db: Session, email: str, password: str):
    user = get_user(db, email=email)
    if not user:
        return False
    if not verify_password(password, user.hashed_password):
        return False
    return user

def get_current_user(db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Kimlik bilgileri doğrulanamadı",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
        token_data = email # Basitçe email'i kullan
    except JWTError:
        raise credentials_exception
        
    user = get_user(db, email=token_data)
    if user is None:
        raise credentials_exception
    return user

def get_current_active_user(current_user: User = Depends(get_current_user)):
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Devre dışı bırakılmış kullanıcı")
    return current_user

# --- YARDIMCI ANALİZ FONKSİYONLARI (Basit simülasyon) ---

def calculate_dna_metrics(transactions: List[Transaction]) -> Dict[str, float]:
    # Burası gerçek analitik motorunun yeridir. Basit ortalamalar döndürüldü.
    if not transactions:
        return {
            "DS_Panic_Threshold": 0.0, "PS_Profit_Skewness": 0.0,
            "HD_Patience_Duration_Hours": 0.0, "VT_Volatility_Tolerance": 0.0,
            "FA_Overtrading_Score": 0.0,
        }
    
    # Basitçe, örnek metrikler hesaplayın
    win_trades = [t for t in transactions if t.is_winning]
    loss_trades = [t for t in transactions if not t.is_winning]
    
    total_trades = len(transactions)
    win_rate = len(win_trades) / total_trades if total_trades else 0
    avg_pnl = sum(t.pnl_pct for t in transactions) / total_trades if total_trades else 0
    avg_duration = sum(t.duration_hours for t in transactions) / total_trades if total_trades else 0
    
    # Bu metrikler için örnek formüller
    panic_threshold = (1 - win_rate) * 100 * (1 + (abs(avg_pnl) * 0.5)) # Simülasyon
    profit_skewness = len(win_trades) / (len(loss_trades) or 1) # Simülasyon
    
    return {
        "DS_Panic_Threshold": min(panic_threshold, 100.0), # %0-100 arasında
        "PS_Profit_Skewness": profit_skewness,
        "HD_Patience_Duration_Hours": avg_duration,
        "VT_Volatility_Tolerance": (1 - avg_duration / 10) * 100, # Basit sim.
        "FA_Overtrading_Score": total_trades / 50, # Her 50 işlemde 1 puan
    }

def generate_weekly_report_summary(transactions: List[Transaction]):
    # Son 7 günün işlemlerini filtrele
    one_week_ago = datetime.utcnow() - timedelta(days=7)
    recent_transactions = [t for t in transactions if t.exit_time > one_week_ago]
    
    if not recent_transactions:
        return {
            "total_trades": 0, "win_rate_pct": 0.0, "avg_pnl_pct": 0.0,
            "avg_duration_hours": 0.0, "avg_volatility_pct": 0.0,
            "analysis_summary": "Bu hafta yeterli işlem yapılmadı. Daha fazla veri toplayın."
        }
        
    total_trades = len(recent_transactions)
    win_trades = [t for t in recent_transactions if t.is_winning]
    
    win_rate_pct = (len(win_trades) / total_trades) * 100
    avg_pnl_pct = sum(t.pnl_pct for t in recent_transactions) / total_trades
    avg_duration_hours = sum(t.duration_hours for t in recent_transactions) / total_trades
    avg_volatility_pct = sum(t.volatility_pct for t in recent_transactions) / total_trades
    
    summary = "İşlemleriniz ortalama üstü bir kâr oranı gösteriyor."
    if win_rate_pct < 40:
        summary = "Kazanma oranınız düşük. Daha seçici olmalısınız."
    elif avg_pnl_pct < 0:
        summary = "Haftayı zararla kapattınız. Zararı kesme stratejinizi gözden geçirin."
        
    return {
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate_pct, 2),
        "avg_pnl_pct": round(avg_pnl_pct, 2),
        "avg_duration_hours": round(avg_duration_hours, 2),
        "avg_volatility_pct": round(avg_volatility_pct, 2),
        "analysis_summary": summary
    }

# --- E-POSTA GÖNDERME FONKSİYONU ---

def send_email_report(recipient_email: str, report_data: Dict[str, Any]):
    # Basit bir e-posta simülasyonu
    
    message = EmailMessage()
    message["Subject"] = "TradeMirror Haftalık Psiko-Metrik Raporunuz"
    message["From"] = EMAIL_SENDER
    message["To"] = recipient_email
    
    body = f"""
    Sayın Kullanıcı,
    
    Bu, haftalık Psiko-Metrik Raporunuzun özetidir:
    
    - Toplam İşlem: {report_data['total_trades']}
    - Kazanma Oranı: {report_data['win_rate_pct']}%
    - Ortalama Kâr/Zarar: {report_data['avg_pnl_pct']}%
    - Analiz Özeti: {report_data['analysis_summary']}
    
    Lütfen daha fazla detay için uygulamaya giriş yapın.
    """
    message.set_content(body)
    
    context = ssl.create_default_context()
    
    try:
        # SMTP Bağlantısını kur
        with smtplib.SMTP_SSL(EMAIL_SMTP_SERVER, 465, context=context) as server:
            # server.ehlo() # Gerekirse
            # server.starttls() # Gerekirse
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, recipient_email, message.as_string())
        print(f"INFO: Rapor e-postası {recipient_email} adresine gönderildi.")
    except Exception as e:
        print(f"UYARI: E-posta gönderimi başarısız oldu: {e}")
        print("Lütfen e-posta ortam değişkenlerini kontrol edin (EMAIL_SENDER, EMAIL_PASSWORD vb.)")


# --- ROUTE: ANA SAYFA YÖNLENDİRMESİ ---

@app.get("/")
def read_root():
    return RedirectResponse(url="/static/login.html")

# --- ROUTE: KULLANICI İŞLEMLERİ (AUTH) ---

@app.post("/api/v1/users/register", response_model=Dict[str, str], tags=["Users"])
def register_user(user: UserCreate, db: Session = Depends(get_db)):
    db_user = get_user(db, email=user.email)
    if db_user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="E-posta adresi zaten kayıtlı.")
        
    hashed_password = get_password_hash(user.password)
    
    new_user = User(
        email=user.email,
        hashed_password=hashed_password,
        is_active=True,
        setup_complete=False # Yeni kullanıcılar kurulumu tamamlamaya zorlanır
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"message": "Kayıt başarılı"}

@app.post("/api/v1/token", response_model=Token, tags=["Auth"])
def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # form_data.username aslında email'dir.
    user = authenticate_user(db, email=form_data.username, password=form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Hatalı kullanıcı adı veya şifre",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.email}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

# --- ROUTE: KURULUM İŞLEMLERİ ---

@app.get("/api/v1/setup/status", tags=["Setup"])
def get_setup_status(current_user: User = Depends(get_current_active_user)):
    """ API anahtarlarının kurulup kurulmadığını kontrol eder. """
    return {"is_setup_complete": current_user.setup_complete}

@app.post("/api/v1/setup/keys", tags=["Setup"])
def save_api_keys(api_keys: APISetup, current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """ Kullanıcının API anahtarlarını şifreleyerek kaydeder. """
    
    # Gerçek uygulamada burada sağlam bir E2E şifreleme mekanizması kullanmalısınız
    # Basitleştirilmiş örnek: Sadece Text olarak kaydet
    
    current_user.api_key_encrypted = api_keys.api_key # Şifreleme simülasyonu
    current_user.api_secret_encrypted = api_keys.api_secret # Şifreleme simülasyonu
    current_user.setup_complete = True
    
    db.commit()
    
    # Celery kuruluysa, arka plan görevini başlat (API Key senkronizasyonu)
    if synchronize_user_trades_task:
        # Gerçek uygulamada burada API Keylerin çözülüp gönderilmesi gerekir
        synchronize_user_trades_task.delay(current_user.user_id, api_keys.api_key, api_keys.api_secret)
    else:
        print("UYARI: Celery kurulu değil. İşlem senkronizasyon görevi başlatılamadı.")
        
    return {"message": "API anahtarları güvenli bir şekilde kaydedildi ve ilk senkronizasyon görevi başlatıldı."}

# --- ROUTE: İŞLEM VE ANALİZ İŞLEMLERİ ---

@app.post("/api/v1/transactions/add", tags=["Transactions"])
def add_transaction(transaction: TransactionCreate, current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """ Manuel işlem kaydı ekler. """
    
    new_transaction = Transaction(
        user_id=current_user.user_id,
        trade_id=transaction.trade_id,
        is_winning=transaction.is_winning,
        pnl_pct=transaction.pnl_pct,
        max_drawdown_pct=transaction.max_drawdown_pct,
        duration_hours=transaction.duration_hours,
        volatility_pct=transaction.volatility_pct,
        exit_time=transaction.exit_time
    )
    
    db.add(new_transaction)
    db.commit()
    db.refresh(new_transaction)
    
    # İşlem eklendikten sonra DNA profilini güncelle
    # Bu, gerçek bir sistemde asenkron bir görev (Celery) olmalıdır.
    
    # Mevcut tüm işlemleri çek
    all_transactions = db.query(Transaction).filter(Transaction.user_id == current_user.user_id).all()
    
    # Metrikleri hesapla
    calculated_metrics = calculate_dna_metrics(all_transactions)
    
    # DNAProfile tablosunu güncelle/oluştur
    dna_profile = db.query(DNAProfile).filter(DNAProfile.user_id == current_user.user_id).first()
    if not dna_profile:
        dna_profile = DNAProfile(user_id=current_user.user_id)
        db.add(dna_profile)
        
    # Basit bir güncelleme simülasyonu (risk toleransını, baskın duyguyu değiştirmez)
    dna_profile.last_updated = datetime.utcnow()
    
    # DNAMetric tablosunu güncelle/oluştur
    for key, value in calculated_metrics.items():
        metric = db.query(DNAMetric).filter(
            DNAMetric.user_id == current_user.user_id,
            DNAMetric.metric_name == key,
            DNAMetric.is_ideal == False # Gerçek profil
        ).first()
        
        if metric:
            metric.value = value
            metric.last_updated = datetime.utcnow()
        else:
            new_metric = DNAMetric(
                user_id=current_user.user_id,
                metric_name=key,
                value=value,
                is_ideal=False
            )
            db.add(new_metric)
            
        # İdeal metrikler için basit bir varsayım (Gerçek sistemde kullanıcıdan alınır)
        ideal_metric = db.query(DNAMetric).filter(
            DNAMetric.user_id == current_user.user_id,
            DNAMetric.metric_name == key,
            DNAMetric.is_ideal == True # İdeal profil
        ).first()
        
        if not ideal_metric:
             ideal_value = value * 1.1 if "Threshold" not in key else value * 0.9 # İdeal metrik varsayımı
             new_ideal_metric = DNAMetric(
                user_id=current_user.user_id,
                metric_name=key,
                value=ideal_value,
                is_ideal=True
            )
             db.add(new_ideal_metric)
        
    db.commit()
    return {"message": "İşlem kaydedildi ve DNA profili güncellendi."}

@app.get("/api/v1/transactions/history", response_model=List[TransactionCreate], tags=["Transactions"])
def get_transaction_history(current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """ Tüm işlem geçmişini döndürür. """
    
    transactions = db.query(Transaction).filter(Transaction.user_id == current_user.user_id).all()
    return transactions

@app.get("/api/v1/dna/profile", tags=["Analysis"])
def get_dna_profile(current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """ Kullanıcının Davranışsal DNA profilini döndürür. """
    
    dna_profile = db.query(DNAProfile).filter(DNAProfile.user_id == current_user.user_id).first()
    
    if not dna_profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DNA profili bulunamadı. Lütfen işlem kaydedin.")
        
    # Gerçek metrikleri al
    current_metrics_list = db.query(DNAMetric).filter(
        DNAMetric.user_id == current_user.user_id,
        DNAMetric.is_ideal == False
    ).all()
    
    # İdeal metrikleri al
    ideal_metrics_list = db.query(DNAMetric).filter(
        DNAMetric.user_id == current_user.user_id,
        DNAMetric.is_ideal == True
    ).all()
    
    # Sözlüğe dönüştür
    current_metrics = {m.metric_name: m.value for m in current_metrics_list}
    ideal_metrics = {m.metric_name: m.value for m in ideal_metrics_list}
    
    # Panik eşiği geçmişi için basit bir simülasyon
    history = [
        {"date": (datetime.utcnow() - timedelta(days=i)).isoformat(), "panic_threshold": random.uniform(50.0, 95.0)}
        for i in range(30)
    ]
    history.reverse()

    return {
        "current_profile": current_metrics,
        "ideal_profile": ideal_metrics,
        "panic_threshold_history": history,
        "risk_tolerance": dna_profile.risk_tolerance,
        "dominant_emotion": dna_profile.dominant_emotion,
        "last_updated": dna_profile.last_updated.isoformat()
    }

@app.get("/api/v1/report/weekly", tags=["Report"])
def get_weekly_report(current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """ Haftalık rapor özetini döndürür. """
    transactions = db.query(Transaction).filter(Transaction.user_id == current_user.user_id).all()
    report = generate_weekly_report_summary(transactions)
    return report

@app.post("/api/v1/report/send", tags=["Report"])
def send_report_email(current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """ Haftalık raporu kullanıcıya e-posta ile gönderir. """
    
    transactions = db.query(Transaction).filter(Transaction.user_id == current_user.user_id).all()
    report_data = generate_weekly_report_summary(transactions)
    
    try:
        # send_email_report fonksiyonu şimdi sizin e-posta adresinizi kullanacak
        send_email_report(current_user.email, report_data)
        return {"message": f"Rapor başarıyla {current_user.email} adresine gönderildi."}
    except Exception as e:
        print(f"E-posta gönderme hatası: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Rapor gönderilirken sunucu hatası oluştu.")
