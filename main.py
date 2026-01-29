import os
import io
import csv
from datetime import datetime, date, time, timedelta, timezone
from enum import Enum

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from passlib.context import CryptContext

from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean,
    DateTime, ForeignKey, select, UniqueConstraint
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session

from openpyxl import Workbook

# -----------------------------
# Config
# -----------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "troque-este-token")
ALLOWED_IPS = [ip.strip() for ip in os.environ.get("ALLOWED_IPS", "").split(",") if ip.strip()]

# Se você souber o seu IP fixo: ALLOWED_IPS="200.200.200.200"
# Pode passar múltiplos: "200.200.200.200,201.201.201.201"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não definido. Configure Postgres na nuvem e defina DATABASE_URL.")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# Ajuste de fuso: Brasil (-03:00). Sem horário de verão atualmente.
BRT = timezone(timedelta(hours=-3))

# -----------------------------
# Models
# -----------------------------
class Acao(str, Enum):
    ENTRADA = "Entrada"
    SAIDA = "Saída"

class Bolsista(Base):
    __tablename__ = "bolsistas"
    __table_args__ = (UniqueConstraint("cpf", name="uq_bolsista_cpf"),)

    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False)
    cpf = Column(String, nullable=False)  # armazenar só dígitos é melhor (mas aqui deixamos simples)
    pin_hash = Column(String, nullable=False)
    ativo = Column(Boolean, default=True, nullable=False)

    registros = relationship("Registro", back_populates="bolsista")

class Registro(Base):
    __tablename__ = "registros"

    id = Column(Integer, primary_key=True)
    bolsista_id = Column(Integer, ForeignKey("bolsistas.id"), nullable=False)
    acao = Column(String, nullable=False)  # "Entrada" / "Saída"
    data_hora = Column(DateTime(timezone=True), nullable=False)
    ip_origem = Column(String, nullable=True)

    bolsista = relationship("Bolsista", back_populates="registros")

def init_db():
    Base.metadata.create_all(bind=engine)

# -----------------------------
# Helpers
# -----------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def normalize_cpf(cpf: str) -> str:
    return "".join([c for c in cpf if c.isdigit()])

def hash_pin(pin: str) -> str:
    return pwd_context.hash(pin)

def verify_pin(pin: str, pin_hash: str) -> bool:
    return pwd_context.verify(pin, pin_hash)

def client_ip(request: Request) -> str:
    """
    Em hospedagem na nuvem, o IP real costuma vir em X-Forwarded-For.
    Pegamos o primeiro IP da lista.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def enforce_ip_allowlist(request: Request):
    if not ALLOWED_IPS:
        # Se você não configurar ALLOWED_IPS, não bloqueia. (Útil para testes)
        return
    ip = client_ip(request)
    if ip not in ALLOWED_IPS:
        raise HTTPException(status_code=403, detail=f"Acesso negado fora do ambiente autorizado. IP={ip}")

def enforce_admin(request: Request):
    token = request.headers.get("x-admin-token")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Não autorizado (admin).")

def brt_now() -> datetime:
    return datetime.now(tz=BRT)

def parse_period(start: str, end: str):
    # start/end no formato YYYY-MM-DD
    try:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
    except Exception:
        raise HTTPException(status_code=400, detail="Use start/end no formato YYYY-MM-DD.")
    if e < s:
        raise HTTPException(status_code=400, detail="end deve ser >= start.")
    # intervalo inclusivo: [start 00:00:00, end 23:59:59]
    start_dt = datetime.combine(s, time(0, 0, 0), tzinfo=BRT)
    end_dt = datetime.combine(e, time(23, 59, 59), tzinfo=BRT)
    return start_dt, end_dt

# -----------------------------
# Schemas
# -----------------------------
class CheckRequest(BaseModel):
    cpf: str = Field(..., description="CPF do bolsista")
    pin: str = Field(..., min_length=4, max_length=8, description="PIN (4 a 8 dígitos)")
    acao: Acao

class AdminBolsistaCreate(BaseModel):
    nome: str
    cpf: str
    pin: str = Field(..., min_length=4, max_length=8)
    ativo: bool = True

# -----------------------------
# App
# -----------------------------
app = FastAPI(title="Controle Presencial - Incubadora")

@app.on_event("startup")
def startup():
    init_db()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/check")
def registrar_ponto(payload: CheckRequest, request: Request, db: Session = Depends(get_db)):
    enforce_ip_allowlist(request)

    cpf = normalize_cpf(payload.cpf)
    pin = payload.pin.strip()

    bolsista = db.execute(select(Bolsista).where(Bolsista.cpf == cpf)).scalar_one_or_none()
    if not bolsista or not bolsista.ativo:
        raise HTTPException(status_code=404, detail="Bolsista não encontrado ou inativo.")
    if not verify_pin(pin, bolsista.pin_hash):
        raise HTTPException(status_code=401, detail="PIN inválido.")

    # Busca último registro para regra simples
    last = db.execute(
        select(Registro)
        .where(Registro.bolsista_id == bolsista.id)
        .order_by(Registro.data_hora.desc())
        .limit(1)
    ).scalar_one_or_none()

    if last and last.acao == payload.acao.value:
        raise HTTPException(status_code=400, detail=f"Você já registrou '{payload.acao.value}' por último. Registre o oposto antes.")

    reg = Registro(
        bolsista_id=bolsista.id,
        acao=payload.acao.value,
        data_hora=brt_now(),
        ip_origem=client_ip(request)
    )
    db.add(reg)
    db.commit()

    return {"ok": True, "nome": bolsista.nome, "acao": reg.acao, "data_hora": reg.data_hora.isoformat()}

@app.post("/admin/bolsistas")
def admin_criar_bolsista(payload: AdminBolsistaCreate, request: Request, db: Session = Depends(get_db)):
    enforce_admin(request)

    cpf = normalize_cpf(payload.cpf)
    if len(cpf) != 11:
        raise HTTPException(status_code=400, detail="CPF inválido (precisa ter 11 dígitos).")

    exists = db.execute(select(Bolsista).where(Bolsista.cpf == cpf)).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail="Já existe bolsista com esse CPF.")

    b = Bolsista(
        nome=payload.nome.strip(),
        cpf=cpf,
        pin_hash=hash_pin(payload.pin.strip()),
        ativo=payload.ativo
    )
    db.add(b)
    db.commit()
    return {"ok": True, "id": b.id, "nome": b.nome}

@app.get("/admin/export.csv")
def admin_export_csv(start: str, end: str, request: Request, db: Session = Depends(get_db)):
    enforce_admin(request)
    start_dt, end_dt = parse_period(start, end)

    rows = db.execute(
        select(Bolsista.nome, Registro.acao, Registro.data_hora)
        .join(Registro, Registro.bolsista_id == Bolsista.id)
        .where(Registro.data_hora >= start_dt, Registro.data_hora <= end_dt)
        .order_by(Registro.data_hora.asc())
    ).all()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Nome", "Ação", "Data/Hora"])
    for nome, acao, dt in rows:
        dt_brt = dt.astimezone(BRT)
        writer.writerow([nome, acao, dt_brt.strftime("%d/%m/%Y %H:%M:%S")])

    output.seek(0)
    filename = f"Ponto_Incubadora_{start}_a_{end}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.get("/admin/export.xlsx")
def admin_export_xlsx(start: str, end: str, request: Request, db: Session = Depends(get_db)):
    enforce_admin(request)
    start_dt, end_dt = parse_period(start, end)

    rows = db.execute(
        select(Bolsista.nome, Registro.acao, Registro.data_hora)
        .join(Registro, Registro.bolsista_id == Bolsista.id)
        .where(Registro.data_hora >= start_dt, Registro.data_hora <= end_dt)
        .order_by(Registro.data_hora.asc())
    ).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Registros"

    ws.append(["Nome", "Ação", "Data/Hora"])
    for nome, acao, dt in rows:
        dt_brt = dt.astimezone(BRT)
        ws.append([nome, acao, dt_brt.strftime("%d/%m/%Y %H:%M:%S")])

    # Congelar cabeçalho e filtro
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:C{ws.max_row}"

    # Ajuste simples de largura
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 20

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)

    filename = f"Ponto_Incubadora_{start}_a_{end}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.get("/")
def home():
    # Um “ping” simples; normalmente você faria uma página HTML
    return JSONResponse({
        "app": "Controle Presencial - Incubadora",
        "endpoints": {
            "check": "POST /check",
            "admin_create_bolsista": "POST /admin/bolsistas (header x-admin-token)",
            "export_xlsx": "GET /admin/export.xlsx?start=YYYY-MM-DD&end=YYYY-MM-DD (header x-admin-token)",
            "export_csv": "GET /admin/export.csv?start=YYYY-MM-DD&end=YYYY-MM-DD (header x-admin-token)"
        }
    })
