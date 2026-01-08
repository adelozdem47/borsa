# app.py - TradeMirror Global Backend (CELERY'SİZ ASYNC ÇÖZÜMÜ)

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

# --- GEREKLİ ASYNC IMPORTLARI (Celery Kaldırıldı) ---
import asyncio
# ccxt kütüphanesinin eşzamansız (async) desteği kullanılır.
try:
    import ccxt.async_support as ccxt_async
except ImportError:
    ccxt_async = None
    print("UYARI: ccxt.async_support içeri aktarılamadı. Lütfen 'ccxt' kütüphanesini kurun.")


# --- E-POSTA İMPORTLARI ---
import smtplib
import ssl
from email.message import EmailMessage

# --- VERİTABANI İMPORTLARI ---
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, func, Boolean
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.exc import OperationalError

# --- GÜVENLİK İMPORTLARI ---
from jose import jwt, JWTError
from passlib.context import CryptContext

# --- ORTAM DEĞİŞKENLERİ VE SABİTLER (Prodüksiyon Ayarları) ---

# KRİTİK: Bağlantı dizesi varsayılan olarak ayarlandı.
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:admin123@localhost:5432/borsa")

# JWT Gizli Anahtarı
SECRET_KEY = os.environ.get("SECRET_KEY", "GÜÇLÜ-UZUN-SECRET-KEY-BURAYA-KOYULMALI-PROD-ORTAMINDA")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

# SMTP (E-posta) Ayarları
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
# E-posta sorununu çözen Port 587 (STARTTLS) kullanıldı.
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "adelozdem6@gmail.com")
# UYARI: Bu şifre varsayılandır. Kendi GMail Uygulama Şifrenizle DEĞİŞTİRİN.
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "yjcu lcld eato zxek")

# --- GÜVENLİK ARAÇLARI ---
pwd_context = CryptContext(schemes=["sha256_crypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/token")

# --- VERİTABANI YAPILANDIRMASI ---
# SSL ve Bağlantı Havuzu istikrarı için gerekli ayarlar.
engine = create_engine(
    DATABASE_URL,
    connect_args={"sslmode": "require"},
    pool_pre_ping=True,
    # Bağlantı kesintilerini önlemek için geri dönüşüm süresi.
    pool_recycle=60
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- VERİTABANI MODELLERİ ---
class User(Base):
    __tablename__ = "users"
    user_id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    setup_complete = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    api_key = Column(Text, nullable=True)
    api_secret = Column(Text, nullable=True)
    exchange = Column(String, nullable=True) # Kullanıcının borsa adı (Örn: Binance, BIST)

class DnaProfile(Base):
    __tablename__ = "dna_profiles"
    profile_id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, unique=True, index=True)
    risk_tolerance = Column(String, default="ORTA")
    dominant_emotion = Column(String, default="Nötr")
    overtrading_tendency = Column(Float, default=0.0)
    patience_score = Column(Float, default=0.0)
    consistency_score = Column(Float, default=0.0)
    last_updated = Column(DateTime, default=datetime.utcnow)

class Transaction(Base):
    __tablename__ = "transactions"
    trade_id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    symbol = Column(String) # Hissenin/Coinin adı (Örn: ADEL, BTC/USDT)
    entry_price = Column(Float)
    exit_price = Column(Float)
    size = Column(Float)
    entry_time = Column(DateTime)
    exit_time = Column(DateTime)
    pnl_pct = Column(Float)
    max_drawdown_pct = Column(Float)
    duration_minutes = Column(Float)
    emotion_at_exit = Column(String)

try:
    Base.metadata.create_all(bind=engine)
except OperationalError as e:
    print(f"KRİTİK HATA: Veritabanı bağlantı hatası! Detay: {e}")


# --- Pydantic Modelleri ---
class UserBase(BaseModel):
    email: EmailStr

class UserCreate(UserBase):
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    email: Optional[str] = None

class SetupApiKey(BaseModel):
    api_key: str
    api_secret: str
    exchange: str

# --- GEREKSİNİM BAĞIMLILIKLARI ve YARDIMCI FONKSİYONLAR ---

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "iat": datetime.utcnow()})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def get_user_by_email(db: Session, email: str):
    return db.query(User).filter(User.email == email).first()

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
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
        token_data = TokenData(email=email)
    except JWTError:
        raise credentials_exception
        
    user = get_user_by_email(db, email=token_data.email)
    if user is None:
        raise credentials_exception
    return user

def get_current_active_user(current_user: User = Depends(get_current_user)):
    if current_user.is_active is not True:
        raise HTTPException(status_code=400, detail="Devre dışı bırakılmış kullanıcı")
    return current_user

# GÜNCEL E-POSTA GÖNDERME FONKSİYONLARI

def send_email_report(recipient_email: str, report_data: Dict[str, Any]):
    """ Kullanıcıya haftalık raporu e-posta ile gönderir. (Port 587 ile düzeltildi) """
    
    if not SMTP_USERNAME or not SMTP_PASSWORD or SMTP_PASSWORD == "yjcu lcld eato zxek":
        raise HTTPException(status_code=500, detail="E-posta servisi yapılandırılmamış. Lütfen SMTP_PASSWORD'a GMail Uygulama Şifrenizi tanımlayın.")

    try:
        msg = EmailMessage()
        msg['Subject'] = 'TradeMirror Global: Haftalık Davranışsal Raporunuz'
        msg['From'] = SMTP_USERNAME
        msg['To'] = recipient_email
        
        content = f"""
        Sayın {recipient_email},
        
        Haftalık Davranışsal Raporunuz hazırdır:

        - Toplam İşlem Sayısı: {report_data.get('total_trades', 0)}
        - Ortalama İşlem Süresi: {report_data.get('avg_duration_hours', 0.0)} saat
        - Baskın Duygu: {report_data.get('dominant_emotion', 'N/A')}
        - Risk Toleransı: {report_data.get('risk_tolerance', 'N/A')}
        
        Detaylı raporu platformunuzda bulabilirsiniz.

        Saygılarımızla,
        TradeMirror Global Ekibi
        """
        msg.set_content(content)
        
        context = ssl.create_default_context()
        
        # Port 587 için SMTP ve starttls() kullanımı
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls(context=context) # TLS şifrelemesini başlatır
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
            
    except Exception as e:
        print(f"E-posta gönderme hatası: {e}")
        # Kullanıcıya ağ veya kimlik doğrulama hatasını bildirir.
        raise HTTPException(status_code=500, detail=f"E-posta gönderme hatası. Ağ/Kimlik doğrulama başarısız (Hata kodu: 500). Detay: {e}")


def send_instant_alert_email(recipient_email: str, alert_data: Dict[str, Any]):
    """ Anlık kritik haber uyarısı e-postası gönderir. (Port 587 ile düzeltildi) """
    
    if not SMTP_USERNAME or not SMTP_PASSWORD or SMTP_PASSWORD == "yjcu lcld eato zxek":
        print("UYARI: SMTP şifresi tanımlı değil. E-posta gönderilemedi.")
        return

    try:
        msg = EmailMessage()
        msg['Subject'] = f"🚨 KRİTİK UYARI: {alert_data.get('alert_title', 'Yeni Gelişme')}"
        msg['From'] = SMTP_USERNAME
        msg['To'] = recipient_email
        
        content = f"""
        Sayın {recipient_email},
        
        Portföyünüzü etkileyebilecek anlık bir gelişme tespit edilmiştir:

        ---
        Başlık: {alert_data.get('alert_title', 'N/A')}
        Etki: {alert_data.get('impact', 'N/A')}
        Duygu: {alert_data.get('sentiment', 'N/A')}
        İlgili Borsa/Piyasa: {alert_data.get('exchange', 'N/A')}
        ---
        
        Lütfen pozisyonlarınızı kontrol edin.

        Saygılarımızla,
        TradeMirror Global Yapay Zeka
        """
        msg.set_content(content)
        
        context = ssl.create_default_context()
        
        # Port 587 (STARTTLS) kullanımı
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls(context=context)
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
            
    except Exception as e:
        raise Exception(f"Anlık bildirim e-posta hatası: {e}")


def generate_weekly_report_summary(transactions: List[Transaction]) -> Dict[str, Any]:
    # Analiz mantığı (Mock veriler kullanılarak)
    total_trades = len(transactions)
    if total_trades == 0:
        return {
            "total_trades": 0, "win_rate_pct": 0.0, "avg_pnl_pct": 0.0,
            "avg_duration_hours": 0.0, "avg_volatility_pct": 0.0,
            "analysis_summary": "Henüz yeterli işlem verisi yok.",
            "risk_tolerance": "ORTA",
            "dominant_emotion": "Nötr"
        }
        
    winning_trades = [t for t in transactions if t.pnl_pct > 0]
    total_pnl = sum(t.pnl_pct for t in transactions)
    total_duration_minutes = sum(t.duration_minutes for t in transactions)
    avg_volatility = random.uniform(0.5, 5.0)
        
    win_rate_pct = (len(winning_trades) / total_trades) * 100
    avg_pnl_pct = total_pnl / total_trades
    avg_duration_hours = total_duration_minutes / total_trades / 60
    
    # Basit bir psiko-metrik analiz özeti
    if win_rate_pct < 40 and avg_duration_hours < 0.5:
        summary = "Hızlı Zarar Kesme eğilimi yüksek. Pozisyonlarda kalma sürenizi uzatın."
    elif win_rate_pct > 60 and avg_duration_hours > 5.0:
        summary = "Sabırlı bir profilsiniz. Risk/Ödül oranınızı optimize etmeyi düşünebilirsiniz."
    else:
        summary = "Dengeli bir profil. Kayıtlı verilerinize göre performansınız stabil."

    return {
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate_pct, 2),
        "avg_pnl_pct": round(avg_pnl_pct, 2),
        "avg_duration_hours": round(avg_duration_hours, 2),
        "avg_volatility_pct": round(avg_volatility, 2),
        "analysis_summary": summary,
        "risk_tolerance": "ORTA" if avg_volatility < 3 else "YÜKSEK",
        "dominant_emotion": "Hırs" if avg_pnl_pct > 5 else "Nötr"
    }

# YENİ KOD: İLK KULLANICIYI OLUŞTURMA FONKSİYONU
def create_initial_admin_user():
    """ Eğer veritabanında kullanıcı yoksa, varsayılan bir yönetici kullanıcı oluşturur. """
    db = SessionLocal()
    INITIAL_EMAIL = "admin@trademirror.com"
    INITIAL_PASSWORD = "admin123"

    try:
        user_exists = get_user_by_email(db, email=INITIAL_EMAIL)
        
        if not user_exists:
            print(f"INFO: '{INITIAL_EMAIL}' kullanıcısı veritabanında bulunamadı. Yeni kullanıcı oluşturuluyor...")
            
            hashed_password = get_password_hash(INITIAL_PASSWORD)
            db_user = User(
                email=INITIAL_EMAIL,
                hashed_password=hashed_password,
                is_active=True,
                setup_complete=False
            )
            
            db.add(db_user)
            db.commit()
            db.refresh(db_user)
            
            # Yeni kullanıcı için DNA profili oluştur
            db_dna = DnaProfile(user_id=db_user.user_id)
            db.add(db_dna)
            db.commit()

            print(f"BAŞARILI: Yönetici kullanıcı ('{INITIAL_EMAIL}') oluşturuldu. Şifre: '{INITIAL_PASSWORD}'")
        else:
             print(f"INFO: Yönetici kullanıcı ('{INITIAL_EMAIL}') zaten mevcut.")
    except Exception as e:
        print(f"HATA: Başlangıç kullanıcı oluşturulurken veya veritabanı sorgulanırken bir hata oluştu: {e}")
    finally:
        db.close()


def generate_mock_dna_metrics(user_id: int) -> Dict[str, float]:
    # Kullanıcı ID'sine göre rastgele ama kararlı (seeded) değerler üretilebilir
    random.seed(user_id * 10)
    
    panic = random.uniform(3.0, 15.0)
    skewness = random.uniform(0.5, 2.5)
    patience = random.uniform(0.5, 24.0)
    vol_tol = random.uniform(1.0, 8.0)
    overtrading = random.uniform(1.0, 10.0)

    return {
        "DS_Panic_Threshold": round(panic, 2),
        "PS_Profit_Skewness": round(skewness, 2),
        "HD_Patience_Duration_Hours": round(patience, 1),
        "VT_Volatility_Tolerance": round(vol_tol, 2),
        "FA_Overtrading_Score": round(overtrading, 1),
    }

def update_dna_profile(user_id: int, db: Session):
    transactions = db.query(Transaction).filter(Transaction.user_id == user_id).all()
    
    if not transactions:
        return
        
    dna_profile = db.query(DnaProfile).filter(DnaProfile.user_id == user_id).first()
    if not dna_profile:
        dna_profile = DnaProfile(user_id=user_id)
        db.add(dna_profile)
        db.commit()
        db.refresh(dna_profile)
    
    report_summary = generate_weekly_report_summary(transactions)
    
    dna_profile.risk_tolerance = report_summary.get("risk_tolerance")
    dna_profile.dominant_emotion = report_summary.get("dominant_emotion")
    
    total_trades = len(transactions)
    dna_profile.overtrading_tendency = min(90.0, 10.0 + total_trades * 2)
    dna_profile.patience_score = max(20.0, 95.0 - total_trades)
    dna_profile.consistency_score = random.uniform(30.0, 90.0)
    dna_profile.last_updated = datetime.utcnow()
    
    db.commit()

# --- YENİ EŞZAMANSIZ VERİ ÇEKME FONKSİYONU (Celery Yerine) ---

async def async_synchronize_user_trades(user_id: int, exchange_name: str, api_key: str, api_secret: str):
    """ CCXT'nin eşzamansız sürümünü kullanarak tek sunucu içinde veri çeker. """
    
    db = SessionLocal()
    
    if not ccxt_async:
        print("ASYNC HATA: ccxt.async_support kurulu değil. Veri çekilemedi.")
        db.close()
        return

    try:
        # ccxt.async_support kullanarak eşzamansız bağlantı kurma
        exchange_class = getattr(ccxt_async, exchange_name.lower())
        exchange = exchange_class({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
        })
        
        # Basitlik için son 7 günün işlemlerini çekiyoruz.
        since = exchange.parse8601((datetime.utcnow() - timedelta(days=7)).isoformat() + 'Z')
        
        # await kullanılarak eşzamansız çağrı yapılır
        trades = await exchange.fetch_my_trades(symbol=None, since=since, limit=100)
        
        new_trades_count = 0
        
        for trade in trades:
            # Mock PNL ve Analiz verileri üretilir
            pnl_pct_mock = (float(trade['price']) * 0.01) * random.uniform(-1, 1)
            max_drawdown_mock = abs(pnl_pct_mock) * random.uniform(0.5, 1.5)
            duration_minutes_mock = random.uniform(10, 300)

            # Veritabanı modeli oluşturulur ve kaydedilir
            db_transaction = Transaction(
                user_id=user_id,
                symbol=trade['symbol'],
                entry_price=trade['price'],
                exit_price=trade['price'], # Basitlik için aynı kullanıldı
                size=trade['amount'],
                entry_time=datetime.fromtimestamp(trade['timestamp'] / 1000),
                exit_time=datetime.fromtimestamp(trade['timestamp'] / 1000) + timedelta(minutes=duration_minutes_mock),
                pnl_pct=round(pnl_pct_mock, 2),
                max_drawdown_pct=round(max_drawdown_mock, 2),
                duration_minutes=round(duration_minutes_mock, 2),
                emotion_at_exit="Otomatik"
            )
            
            db.add(db_transaction)
            new_trades_count += 1
            
        db.commit()
        # DNA profilini güncelle
        update_dna_profile(user_id, db)
        
        print(f"ASYNC: Kullanıcı {user_id} için {exchange_name} borsasından {new_trades_count} yeni işlem senkronize edildi.")
        
        await exchange.close()
        
    except Exception as e:
        db.rollback()
        print(f"ASYNC HATA: Kullanıcı {user_id} için veri çekme başarısız: {e}")
        
    finally:
        db.close()

# --- YENİ EŞZAMANSIZ HABER İZLEME VE BİLDİRİM FONKSİYONU ---

async def ai_news_monitor_and_notify():
    """ 
    Periyodik olarak tüm kullanıcıların hisselerini izler ve kritik haberleri e-posta ile bildirir.
    Bu, FastAPI başlatıldığında sonsuz bir arka plan döngüsü olarak çalışacaktır.
    """
    
    # Haber kontrol döngüsünü başlatmak için bekleme süresi (Her 5 dakikada bir kontrol)
    CHECK_INTERVAL_SECONDS = 300
    
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        print(f"\nINFO: Yapay Zeka Haber Monitörü çalışıyor... (Her {CHECK_INTERVAL_SECONDS} saniyede bir)")
        
        db = SessionLocal()
        try:
            # Sadece aktif ve kurulumu tamamlanmış kullanıcıları çek
            users = db.query(User).filter(User.is_active == True, User.setup_complete == True).all()
            
            for user in users:
                
                # Kullanıcının izlemesi gereken anahtar kelimeleri oluştur.
                # Gerçekte, kullanıcının Transaction tablosundan en çok işlem yaptığı semboller çekilir.
                monitored_keywords = []
                
                # 1. Kullanıcının borsasını/piyasasını ekle (Örn: BIST)
                if user.exchange:
                     monitored_keywords.append(user.exchange.upper())
                     
                # 2. Örnek olarak sabit hisse ismini ekle (Gerçekte dinamik olmalı)
                monitored_keywords.extend(["ADEL KALEM", "BIST", "NASDAQ", "KRİPTO PİYASASI"])
                
                monitored_keywords = list(set(monitored_keywords)) # Tekrarları kaldır
                
                if not monitored_keywords:
                    continue
                
                # MOCK Haber Kaynağı (Gerçekte buradan API, RSS veya Web Scraper ile veri çekilir)
                mock_news_feed = [
                    {"source": "Twitter/X", "title": f"Küresel çapta faiz artışı bekleniyor. {user.exchange.upper()} için ani satış riski.", "sentiment": "NEGATİF", "impact": "YÜKSEK"},
                    {"source": "Yerel Ekonomi", "title": "ADEL KALEM Hisseleri hakkında yeni bir yatırım teşvik haberi yayımlandı.", "sentiment": "POZİTİF", "impact": "YÜKSEK"},
                    {"source": "Reuters", "title": "BIST 100 endeksi günü yatay seyirle kapattı. Piyasa sakin.", "sentiment": "NÖTR", "impact": "DÜŞÜK"},
                    {"source": "Global Ekonomi", "title": f"Çin ekonomisi beklentileri aştı, {user.exchange.upper()} genel piyasaya olumlu tepki verdi.", "sentiment": "POZİTİF", "impact": "ORTA"},
                    {"source": "Twitter/X", "title": "Piyasa etkisi düşük bir tweet.", "sentiment": "NÖTR", "impact": "DÜŞÜK"},
                    {"source": "Yerel Ekonomi", "title": "Sektör genelinde kârlılık azaldı. ADEL KALEM dikkatli olmalı.", "sentiment": "NEGATİF", "impact": "ORTA"},
                ]
                
                critical_alerts = []
                
                for news in mock_news_feed:
                    # Basit Anahtar Kelime Eşleştirme (Yapay Zeka taklidi)
                    is_relevant = any(keyword in news['title'].upper() for keyword in monitored_keywords)
                    
                    # Kritiklik Kontrolü: Yüksek veya Orta Etkili ve Duygusal yükü olan (Nötr olmayan) haberleri seç
                    is_critical = (news['impact'] in ["YÜKSEK", "ORTA"]) and (news['sentiment'] in ["POZİTİF", "NEGATİF"])
                    
                    if is_relevant and is_critical:
                        critical_alerts.append(news)
                        
                # Bildirim Gönderme
                for alert in critical_alerts:
                    print(f"BİLDİRİM: Kullanıcı {user.email} için kritik uyarı: {alert['title']} ({alert['sentiment']})")
                    
                    alert_content = {
                        "alert_title": alert['title'],
                        "sentiment": alert['sentiment'],
                        "impact": alert['impact'],
                        "exchange": user.exchange if user.exchange else "Genel Piyasa"
                    }
                    
                    try:
                        send_instant_alert_email(user.email, alert_content)
                    except Exception as e:
                        print(f"E-posta gönderme hatası (Anlık Bildirim): {user.email} -> {e}")
                        
        except Exception as e:
            print(f"GENEL MONİTÖR HATA: {e}")
        finally:
            db.close()

# --- İLK BAŞLANGIÇ GÖREVLERİ ---
try:
    create_initial_admin_user()
except Exception as e:
    print(f"KRİTİK HATA: Veritabanı bağlantısı yapılamadı! Detay: {e}")


# --- FASTAPI UYGULAMASI ---
app = FastAPI(
    title="TradeMirror Global API",
    description="Davranışsal Analiz ve Kişiselleştirilmiş Haber Sistemi",
    version="1.0.0",
)

# KRİTİK: Uygulama başlatıldığında arka plan görevini başlatın
@app.on_event("startup")
async def startup_event():
    # Haber monitörünü arayüzü engellemeyen arka plan görevi olarak başlatır
    asyncio.create_task(ai_news_monitor_and_notify())
    print("INFO: Yapay Zeka Haber Monitörü arka planda başlatıldı.")

# --- UZAKTAN ERİŞİM ÇÖZÜMÜ ---
app.mount("/static", StaticFiles(directory="."), name="static")

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/static/login.html")

# --- CORS AYARLARI ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- AUTH ROTALARI ---

@app.post("/api/v1/token", response_model=Token, tags=["Auth"])
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = get_user_by_email(db, email=form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Hatalı e-posta veya şifre",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.email}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/api/v1/users/register", tags=["Auth"])
def register_user(user: UserCreate, db: Session = Depends(get_db)):
    db_user = get_user_by_email(db, email=user.email)
    if db_user:
        raise HTTPException(status_code=400, detail="Bu e-posta adresi zaten kayıtlı.")
        
    hashed_password = get_password_hash(user.password)
    db_user = User(email=user.email, hashed_password=hashed_password, setup_complete=False)
    
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    
    # Yeni kullanıcı için DNA profili oluştur
    db_dna = DnaProfile(user_id=db_user.user_id)
    db.add(db_dna)
    db.commit()
    
    return {"message": "Kayıt başarılı"}

# --- KULLANICI / AYAR ROTALARI ---

@app.get("/api/v1/users/me", response_model=UserBase, tags=["User"])
def read_users_me(current_user: User = Depends(get_current_active_user)):
    return current_user

@app.post("/api/v1/setup/apikey", tags=["Setup"])
async def setup_api_key(setup_data: SetupApiKey, current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """ Kullanıcının borsa API anahtarlarını kaydeder ve arka plan senkronizasyonunu başlatır. """
    
    current_user.api_key = setup_data.api_key
    current_user.api_secret = setup_data.api_secret
    current_user.exchange = setup_data.exchange
    current_user.setup_complete = True # Kurulum tamamlandı olarak işaretlendi
    
    db.commit()
    
    # KRİTİK DEĞİŞİKLİK: asyncio kullanarak arka plan görevi başlatma (Celery yerine)
    if ccxt_async:
        asyncio.create_task(
            async_synchronize_user_trades(
                current_user.user_id,
                setup_data.exchange,
                setup_data.api_key,
                setup_data.api_secret
            )
        )
    else:
        # Eğer ccxt kurulu değilse veya import edilemediyse uyarı verilir
        print("UYARI: ccxt kütüphanesi kurulu olmadığı için otomatik senkronizasyon başlatılamadı.")
        
    return {"message": "API Anahtarları başarıyla kaydedildi. Arka plan senkronizasyonu başladı."}

@app.get("/api/v1/setup/status", tags=["Setup"])
def check_setup_status(current_user: User = Depends(get_current_active_user)):
    """ API key kurulum durumunu kontrol eder. """
    is_setup = current_user.setup_complete
    return {"is_setup": is_setup, "exchange": current_user.exchange}


# --- İŞLEM ROTALARI ---

class TransactionManualCreate(BaseModel):
    trade_id: str
    is_winning: bool
    duration_hours: float
    pnl_pct: float
    max_drawdown_pct: float
    volatility_pct: float

@app.post("/api/v1/transactions/add", tags=["Transactions"])
def add_transaction_manual(
    transaction: TransactionManualCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    
    exit_time = datetime.utcnow()
    entry_time = exit_time - timedelta(hours=transaction.duration_hours)
    
    pnl_val = transaction.pnl_pct
    
    db_transaction = Transaction(
        user_id=current_user.user_id,
        symbol="MANUAL/USDT",
        entry_price=100.0,
        exit_price=100.0 * (1 + pnl_val / 100),
        size=1.0,
        entry_time=entry_time,
        exit_time=exit_time,
        pnl_pct=round(pnl_val, 2),
        max_drawdown_pct=round(transaction.max_drawdown_pct, 2),
        duration_minutes=round(transaction.duration_hours * 60, 2),
        emotion_at_exit="Nötr"
    )
    
    db.add(db_transaction)
    db.commit()
    
    # DNA profilini güncelle
    update_dna_profile(current_user.user_id, db)
    
    return {"message": "İşlem başarıyla kaydedildi"}

@app.get("/api/v1/transactions/history", tags=["Transactions"])
def get_transaction_history(current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    transactions = db.query(Transaction).filter(Transaction.user_id == current_user.user_id).order_by(Transaction.exit_time.desc()).all()
    
    history_list = []
    for t in transactions:
        is_winning = t.pnl_pct > 0
        history_list.append({
            'trade_id': t.trade_id,
            'is_winning': is_winning,
            'pnl_pct': round(t.pnl_pct, 2),
            'max_drawdown_pct': round(t.max_drawdown_pct, 2),
            'symbol': t.symbol,
            'entry_time': t.entry_time.isoformat(),
            'exit_time': t.exit_time.isoformat(),
            'emotion_at_exit': t.emotion_at_exit,
            'duration_hours': round(t.duration_minutes / 60, 2)
        })
    
    return history_list

# --- DNA / RAPOR ROTALARI ---

@app.get("/api/v1/dna/profile", tags=["DNA"])
def get_dna_profile(current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """ Davranışsal DNA profilini döndürür. """
    dna_profile = db.query(DnaProfile).filter(DnaProfile.user_id == current_user.user_id).first()
    if not dna_profile:
        dna_profile = DnaProfile(user_id=current_user.user_id)
        db.add(dna_profile)
        db.commit()
        db.refresh(dna_profile)
        
    current_metrics = generate_mock_dna_metrics(current_user.user_id)
    ideal_metrics = generate_mock_dna_metrics(1000)
    
    history = []
    for i in range(7):
        date = datetime.now() - timedelta(days=i)
        panic_threshold = current_metrics["DS_Panic_Threshold"] + random.uniform(-2, 2)
        history.append({
            "date": date.isoformat(),
            "panic_threshold": round(max(0.1, panic_threshold), 2)
        })
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
        send_email_report(current_user.email, report_data)
        return {"message": f"Rapor başarıyla {current_user.email} adresine gönderildi."}
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
