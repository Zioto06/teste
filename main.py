import os
import io
import csv
from datetime import datetime, date, time, timedelta, timezone
from enum import Enum
from typing import Dict, Tuple, Optional, List

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from sqlalchemy import create_engine, Column, Integer, String, DateTime, select
from sqlalchemy.orm import sessionmaker, declarative_base, Session

from openpyxl import Workbook

# -----------------------------
# Config
# -----------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "admin-incubadora-2026")
ALLOWED_IPS = [ip.strip() for ip in os.environ.get("ALLOWED_IPS", "").split(",") if ip.strip()]

# Ajuste de fuso: Brasil (-03:00)
BRT = timezone(timedelta(hours=-3))

# Caminho do arquivo usuarios.txt (no root do projeto)
USUARIOS_FILE = os.environ.get("USUARIOS_FILE", "usuarios.txt")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não definido. Configure Postgres e defina DATABASE_URL.")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

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
    return "".join([c for c in (cpf or "") if c.isdigit()])

def normalize_pin(pin: str) -> str:
    return "".join([c for c in (pin or "") if c.isdigit()])

def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def enforce_ip_allowlist(request: Request):
    if not ALLOWED_IPS:
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
    try:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
    except Exception:
        raise HTTPException(status_code=400, detail="Use start/end no formato YYYY-MM-DD.")
    if e < s:
        raise HTTPException(status_code=400, detail="end deve ser >= start.")
    start_dt = datetime.combine(s, time(0, 0, 0), tzinfo=BRT)
    end_dt = datetime.combine(e, time(23, 59, 59), tzinfo=BRT)
    return start_dt, end_dt

def load_users_from_txt(filepath: str) -> Dict[str, Tuple[str, str]]:
    """
    Lê usuarios.txt e devolve: { cpf: (nome, pin) }

    Aceita 2 formatos (um por linha):
    1) CSV com ;  -> Nome;CPF;PIN
    2) CSV com ,  -> Nome,CPF,PIN

    Ignora:
    - linhas vazias
    - linhas iniciadas com # (comentário)
    - cabeçalho tipo: nome;cpf;pin
    """
    if not os.path.exists(filepath):
        raise RuntimeError(f"Arquivo de usuários não encontrado: {filepath}")

    users: Dict[str, Tuple[str, str]] = {}
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        # detecta separador
        sep = ";" if ";" in line else ("," if "," in line else None)
        if not sep:
            # linha inválida
            continue

        parts = [p.strip() for p in line.split(sep)]
        if len(parts) < 3:
            continue

        nome, cpf, pin = parts[0], parts[1], parts[2]

        # pula cabeçalho
        if nome.lower() in ("nome", "name") and cpf.lower() in ("cpf",) and pin.lower() in ("pin", "senha"):
            continue

        cpf_n = normalize_cpf(cpf)
        pin_n = normalize_pin(pin)

        if len(cpf_n) != 11:
            continue
        if not (4 <= len(pin_n) <= 8):
            continue

        users[cpf_n] = (nome.strip(), pin_n)

    return users

# cache simples (recarrega se arquivo mudar)
_USERS_CACHE: Optional[Dict[str, Tuple[str, str]]] = None
_USERS_MTIME: Optional[float] = None

def get_users() -> Dict[str, Tuple[str, str]]:
    global _USERS_CACHE, _USERS_MTIME
    mtime = os.path.getmtime(USUARIOS_FILE)
    if _USERS_CACHE is None or _USERS_MTIME != mtime:
        _USERS_CACHE = load_users_from_txt(USUARIOS_FILE)
        _USERS_MTIME = mtime
    return _USERS_CACHE

# -----------------------------
# Models
# -----------------------------
class Acao(str, Enum):
    ENTRADA = "Entrada"
    SAIDA = "Saída"

class Registro(Base):
    __tablename__ = "registros"

    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False)
    cpf = Column(String, nullable=False)  # só dígitos
    acao = Column(String, nullable=False)  # "Entrada" / "Saída"
    data_hora = Column(DateTime(timezone=True), nullable=False)
    ip_origem = Column(String, nullable=True)

def init_db():
    Base.metadata.create_all(bind=engine)

# -----------------------------
# Schemas
# -----------------------------
class CheckRequest(BaseModel):
    cpf: str = Field(..., description="CPF do usuário (somente números ou com máscara)")
    pin: str = Field(..., min_length=4, max_length=8, description="PIN (4 a 8 dígitos)")
    acao: Acao

# -----------------------------
# App
# -----------------------------
app = FastAPI(title="Controle Presencial - Incubadora")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
def startup():
    init_db()
    # Força uma leitura inicial (ajuda a dar erro logo se o arquivo estiver faltando)
    _ = get_users()

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/info")
def info():
    return {
        "app": "Controle Presencial - Incubadora",
        "ui": "/",
        "docs": "/docs",
        "health": "/health",
        "users_source": USUARIOS_FILE,
        "endpoints": {
            "check": "POST /check",
            "admin_export_xlsx": "GET /admin/export.xlsx?start=YYYY-MM-DD&end=YYYY-MM-DD (header x-admin-token)",
            "admin_export_csv": "GET /admin/export.csv?start=YYYY-MM-DD&end=YYYY-MM-DD (header x-admin-token)",
            "admin_export_json": "GET /admin/export.json?start=YYYY-MM-DD&end=YYYY-MM-DD (header x-admin-token)"
        }
    }

@app.post("/check")
def registrar_ponto(payload: CheckRequest, request: Request, db: Session = Depends(get_db)):
    enforce_ip_allowlist(request)

    cpf = normalize_cpf(payload.cpf)
    pin = normalize_pin(payload.pin)

    if len(cpf) != 11:
        raise HTTPException(status_code=400, detail="CPF inválido (precisa ter 11 dígitos).")
    if not (4 <= len(pin) <= 8):
        raise HTTPException(status_code=400, detail="PIN inválido (precisa ter 4 a 8 dígitos).")

    users = get_users()
    if cpf not in users:
        raise HTTPException(status_code=404, detail="Usuário não cadastrado no usuarios.txt.")

    nome_cadastrado, pin_cadastrado = users[cpf]
    if pin != pin_cadastrado:
        raise HTTPException(status_code=401, detail="PIN incorreto.")

    # regra: não permitir Entrada->Entrada ou Saída->Saída
    last = db.execute(
        select(Registro)
        .where(Registro.cpf == cpf)
        .order_by(Registro.data_hora.desc())
        .limit(1)
    ).scalar_one_or_none()

    if last and last.acao == payload.acao.value:
        raise HTTPException(
            status_code=400,
            detail=f"Último registro foi '{last.acao}'. Você precisa registrar o oposto antes."
        )

    reg = Registro(
        nome=nome_cadastrado,
        cpf=cpf,
        acao=payload.acao.value,
        data_hora=brt_now(),
        ip_origem=client_ip(request)
    )
    db.add(reg)
    db.commit()

    return {
        "ok": True,
        "nome": reg.nome,
        "cpf": reg.cpf,
        "acao": reg.acao,
        "data_hora": reg.data_hora.isoformat()
    }

# -----------------------------
# Admin Exports (para usar no /docs)
# -----------------------------
@app.get("/admin/export.json")
def admin_export_json(start: str, end: str, request: Request, db: Session = Depends(get_db)):
    enforce_admin(request)
    start_dt, end_dt = parse_period(start, end)

    rows = db.execute(
        select(Registro.nome, Registro.cpf, Registro.acao, Registro.data_hora, Registro.ip_origem)
        .where(Registro.data_hora >= start_dt, Registro.data_hora <= end_dt)
        .order_by(Registro.data_hora.asc())
    ).all()

    data = []
    for nome, cpf, acao, dt, ip in rows:
        dt_brt = dt.astimezone(BRT)
        data.append({
            "nome": nome,
            "cpf": cpf,
            "acao": acao,
            "data_hora": dt_brt.isoformat(),
            "ip_origem": ip
        })

    return {"start": start, "end": end, "total": len(data), "registros": data}

@app.get("/admin/export.csv")
def admin_export_csv(start: str, end: str, request: Request, db: Session = Depends(get_db)):
    enforce_admin(request)
    start_dt, end_dt = parse_period(start, end)

    rows = db.execute(
        select(Registro.nome, Registro.cpf, Registro.acao, Registro.data_hora)
        .where(Registro.data_hora >= start_dt, Registro.data_hora <= end_dt)
        .order_by(Registro.data_hora.asc())
    ).all()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Nome", "CPF", "Ação", "Data/Hora"])
    for nome, cpf, acao, dt in rows:
        dt_brt = dt.astimezone(BRT)
        writer.writerow([nome, cpf, acao, dt_brt.strftime("%d/%m/%Y %H:%M:%S")])

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
        select(Registro.nome, Registro.cpf, Registro.acao, Registro.data_hora)
        .where(Registro.data_hora >= start_dt, Registro.data_hora <= end_dt)
        .order_by(Registro.data_hora.asc())
    ).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Registros"

    ws.append(["Nome", "CPF", "Ação", "Data/Hora"])
    for nome, cpf, acao, dt in rows:
        dt_brt = dt.astimezone(BRT)
        ws.append([nome, cpf, acao, dt_brt.strftime("%d/%m/%Y %H:%M:%S")])

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:D{ws.max_row}"

    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 20

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)

    filename = f"Ponto_Incubadora_{start}_a_{end}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

