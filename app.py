from groq import Groq
import json
from flask import Flask, render_template, request, abort, send_file, url_for, redirect, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import SQLAlchemyError
import json
import os
import io
import re
import unicodedata
import urllib.error
import urllib.request
from difflib import SequenceMatcher
import qrcode
from datetime import datetime
import uuid
from urllib.parse import quote, urlparse, parse_qsl, urlencode, urlunparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from werkzeug.utils import secure_filename
from jinja2 import TemplateNotFound

BASE_DIR = os.path.dirname(__file__)


def carregar_variaveis_arquivo_env(caminho_env):
    """Carrega variáveis de ambiente de um arquivo .env simples."""
    if not os.path.exists(caminho_env):
        return

    try:
        with open(caminho_env, "r", encoding="utf-8") as f:
            for linha in f:
                texto = linha.strip()
                if not texto or texto.startswith("#") or "=" not in texto:
                    continue

                chave, valor = texto.split("=", 1)
                chave = chave.strip()
                valor = valor.strip()
                if not chave:
                    continue

                if (valor.startswith('"') and valor.endswith('"')) or (valor.startswith("'") and valor.endswith("'")):
                    valor = valor[1:-1]

                if chave not in os.environ:
                    os.environ[chave] = valor
    except OSError:
        return


carregar_variaveis_arquivo_env(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "trocar-em-producao")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# --- ARQUIVOS LOCAIS ---
# O arquivo de exercícios continua local pois é o seu catálogo fixo
DATA_FILE = os.path.join(BASE_DIR, "exercicios.json")
# Vamos manter os caminhos antigos caso você precise fazer a migração dos dados velhos depois
TREINOS_FILE = os.path.join(BASE_DIR, "treinos.json")
QUESTIONARIO_FILE = os.path.join(BASE_DIR, "questionario_respostas.json")
FAVORITOS_FILE = os.path.join(BASE_DIR, "favoritos_admin.json")
EXERCICIOS_CATEGORIAS_FILE = os.path.join(BASE_DIR, "exercicios_categorias.json")
EXERCICIOS_STATIC_DIR = os.path.join(BASE_DIR, "static", "exercicios")
MIDIAS_PERMITIDAS = {".gif", ".mp4"}
PADRAO_ID_EXERCICIO = re.compile(r"^\d{9}$")
GRUPOS_CODIGOS_PADRAO = {
    "cardio": "1",
    "corpo inteiro": "2",
    "membros inferiores": "3",
    "membros superiores": "4",
    "tronco": "5",
}


def garantir_sslmode_require(database_url):
    """Garante sslmode=require para conexões PostgreSQL quando ausente."""
    if not database_url.startswith("postgresql://"):
        return database_url

    parsed = urlparse(database_url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "sslmode" in params:
        return database_url

    params["sslmode"] = "require"
    nova_query = urlencode(params)
    return urlunparse(parsed._replace(query=nova_query))


def mascara_database_url(database_url):
    """Masca senha da URL para logging seguro."""
    if not database_url:
        return ""
    try:
        parsed = urlparse(database_url)
        if parsed.password:
            netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
            return urlunparse(parsed._replace(netloc=netloc))
        return database_url
    except ValueError:
        return "<database_url_invalida>"


def resolver_database_url():
    """Resolve URL do banco em provedores diferentes (Neon/Render/etc)."""
    # Preferência por variáveis específicas do projeto/provedor.
    candidatos_prioritarios = [
        "GYM_DATABASE_URL",
        "NEON_DATABASE_URL",
        "NEONDB_URL",
        "NEONDB",
        "RENDER_DATABASE_URL",
        "EXTERNAL_DATABASE_URL",
                "INTERNAL_DATABASE_URL",
        "SQLALCHEMY_DATABASE_URI",
        "DATABASE_URL",
        "POSTGRES_URL",
    ]

    candidatos_encontrados = []
    origem = ""
    valor = ""
    
    for nome in candidatos_prioritarios:
        candidato = (os.environ.get(nome) or "").strip()
        if not candidato:
            continue
        candidatos_encontrados.append(nome)
        if not valor:
            origem = nome
            valor = candidato

    if not valor:
        for nome, candidato in os.environ.items():
            chave = (nome or "").strip().upper()
            texto = (candidato or "").strip()
            if not texto:
                continue
            if "DATABASE_URL" in chave or chave.endswith("_DB_URL"):
                origem = nome
                valor = texto
                candidatos_encontrados.append(nome)
                break

    if not valor:
        # Fallback para ambientes que expõem apenas PG* vars
        pg_host = (os.environ.get("PGHOST") or "").strip()
        pg_db = (os.environ.get("PGDATABASE") or "").strip()
        pg_user = (os.environ.get("PGUSER") or "").strip()
        pg_password = (os.environ.get("PGPASSWORD") or "").strip()
        pg_port = (os.environ.get("PGPORT") or "5432").strip()

        if pg_host and pg_db and pg_user and pg_password:
            origem = "PG*"
            valor = f"postgresql://{pg_user}:{quote(pg_password)}@{pg_host}:{pg_port}/{pg_db}"

    if valor.startswith("postgres://"):
        valor = f"postgresql://{valor[len('postgres://'):]}"

    valor = garantir_sslmode_require(valor)
    return valor, origem, candidatos_encontrados


database_url, database_url_origem, database_urls_detectadas = resolver_database_url()
if database_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.logger.info(
        f"Banco configurado via {database_url_origem or 'env'}: {mascara_database_url(database_url)}"
    )
    if len(set(database_urls_detectadas)) > 1:
        app.logger.warning(
            "Foram encontradas múltiplas variáveis de banco (%s). Usando %s.",
            ", ".join(sorted(set(database_urls_detectadas))),
            database_url_origem,
        )
else:
    app.logger.warning("DATABASE_URL não encontrada; dados serão salvos localmente e podem ser perdidos no deploy.")

db = SQLAlchemy(app) if database_url else None

if db:
    class TreinoDB(db.Model):
        __tablename__ = "treinos"

        id = db.Column(db.String, primary_key=True)
        link_id = db.Column(db.String, unique=True, nullable=False, index=True)
        dados = db.Column(db.JSON, nullable=False)


    class RespostaDB(db.Model):
        __tablename__ = "respostas"

        id = db.Column(db.String, primary_key=True)
        dados = db.Column(db.JSON, nullable=False)


    class ExercicioDB(db.Model):
        __tablename__ = "exercicios"

        id = db.Column(db.String, primary_key=True)
        dados = db.Column(db.JSON, nullable=False)


    class ExercicioMidiaDB(db.Model):
        __tablename__ = "exercicios_midias"

        exercicio_id = db.Column(db.String, primary_key=True)
        extensao = db.Column(db.String, nullable=False)
        mimetype = db.Column(db.String, nullable=False)
        conteudo = db.Column(db.LargeBinary, nullable=False)


else:
    TreinoDB = None
    RespostaDB = None
    ExercicioDB = None
    ExercicioMidiaDB = None


def montar_url_midia_exercicio(exercicio_id, extensao):
    ex_id = (exercicio_id or "").strip()
    ext = (extensao or "").strip().lower()
    if not ex_id or ext not in MIDIAS_PERMITIDAS:
        return ""
    return f"/midia/exercicios/{ex_id}{ext}"


def salvar_midia_exercicio(exercicio_id, upload, extensao):
    """Salva mídia no banco quando disponível; fallback para arquivo estático."""
    ex_id = (exercicio_id or "").strip()
    ext = (extensao or "").strip().lower()
    if not ex_id or ext not in MIDIAS_PERMITIDAS:
        return ""

    nome_arquivo = secure_filename(f"{ex_id}{ext}")

    if db and ExercicioMidiaDB:
        conteudo = upload.read()
        if not conteudo:
            return ""

        mimetype = (upload.mimetype or "").strip().lower()
        if mimetype not in {"video/mp4", "image/gif"}:
            mimetype = "video/mp4" if ext == ".mp4" else "image/gif"

        try:
            registro = ExercicioMidiaDB.query.get(ex_id)
            if registro:
                registro.extensao = ext
                registro.mimetype = mimetype
                registro.conteudo = conteudo
            else:
                db.session.add(
                    ExercicioMidiaDB(
                        exercicio_id=ex_id,
                        extensao=ext,
                        mimetype=mimetype,
                        conteudo=conteudo,
                    )
                )
            db.session.commit()
            return montar_url_midia_exercicio(ex_id, ext)
        except SQLAlchemyError:
            db.session.rollback()
        finally:
            upload.stream.seek(0)

    pasta_midia = (request.form.get("pasta_midia") or "").strip()
    if not pasta_midia:
        return ""

    pasta_destino = os.path.join(EXERCICIOS_STATIC_DIR, pasta_midia)
    os.makedirs(pasta_destino, exist_ok=True)
    caminho_arquivo = os.path.join(pasta_destino, nome_arquivo)
    upload.save(caminho_arquivo)
    return f"/static/exercicios/{pasta_midia}/{nome_arquivo}"


def caminho_arquivo(nome_constante, fallback_nome_arquivo):
    caminho = globals().get(nome_constante)
    if caminho:
        return caminho
    return os.path.join(BASE_DIR, fallback_nome_arquivo)

TIPOS_TREINO = ["A", "B", "C", "D"]
OBJETIVOS_TREINO = [
    "Hipertrofia",
    "Emagrecimento",
    "Condicionamento",
    "Força",
    "Reabilitação",
    "Hipertrofia e emagrecimento",
    "Hipertrofia e condicionamento",
    "Força e hipertrofia",
    "Força e emagrecimento",
]

MUSCULOS_ALVO_PADRAO = [
    "Peito",
    "Costas",
    "Tríceps",
    "Bíceps",
    "Ombros",
    "Pernas",
    "Quadríceps",
    "Posterior de coxa",
    "Glúteos",
    "Panturrilhas",
    "Abdômen",
]



def admin_ativo():
    return session.get("admin_logado") is True


def exigir_admin_ou_redirect():
    if admin_ativo():
        return None
    return redirect(url_for("admin_login", proximo=request.full_path if request.query_string else request.path))


@app.context_processor
def injetar_estado_admin():
    return {"is_admin": admin_ativo()}

def carregar_exercicios():
    """Carrega a lista de exercícios (banco quando disponível, senão JSON)."""
    data = []

    if db and ExercicioDB:
        try:
            registros = ExercicioDB.query.order_by(ExercicioDB.id.asc()).all()
            data = [r.dados for r in registros if isinstance(r.dados, dict)]
        except SQLAlchemyError:
            data = []

    if not data:
        if not os.path.exists(DATA_FILE):
            return []
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    if not isinstance(data, list):
        return []

    # Garantias mínimas de consistência
    exercicios_normalizados = []

    for ex in data:
        if not isinstance(ex, dict):
            continue
        ex.setdefault("id", "")
        ex.setdefault("nome", "Sem nome")
        ex.setdefault("nome_original", "")
        ex.setdefault("grupo", "Sem grupo")
        ex.setdefault("subgrupo", "Sem subgrupo")
        ex.setdefault("midia", "")
        ex.setdefault("dicas", [])
        ex.setdefault("erros", [])
        ex.setdefault("observacoes", "")
        ex.setdefault("aparelho", "Peso livre")
        exercicios_normalizados.append(ex)

    return exercicios_normalizados


def carregar_favoritos_admin():
    """Carrega IDs de exercícios favoritos do admin."""
    favoritos_file = caminho_arquivo("FAVORITOS_FILE", "favoritos_admin.json")
    if not os.path.exists(favoritos_file):
        return set()

    try:
        with open(favoritos_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return set()

    if not isinstance(data, list):
        return set()

    return {
        (str(ex_id).strip())
        for ex_id in data
        if str(ex_id).strip()
    }


def salvar_favoritos_admin(favoritos_ids):
    favoritos_file = caminho_arquivo("FAVORITOS_FILE", "favoritos_admin.json")
    favoritos_ordenados = sorted({(str(ex_id).strip()) for ex_id in favoritos_ids if str(ex_id).strip()})
    with open(favoritos_file, "w", encoding="utf-8") as f:
        json.dump(favoritos_ordenados, f, ensure_ascii=False, indent=2)


def salvar_exercicios(exercicios):
    if db and ExercicioDB:
        try:
            db.session.query(ExercicioDB).delete()
            for ex in exercicios:
                if not isinstance(ex, dict):
                    continue
                ex_id = str(ex.get("id") or "").strip()
                if not ex_id:
                    continue
                db.session.add(ExercicioDB(id=ex_id, dados=ex))
            db.session.commit()
            return
        except SQLAlchemyError:
            db.session.rollback()
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(exercicios, f, ensure_ascii=False, indent=2)


def slug_texto(texto):
    base = unicodedata.normalize("NFKD", (texto or "")).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return base


def normalizar_linhas_texto(texto):
    return [linha.strip() for linha in (texto or "").splitlines() if linha.strip()]


def normalizar_texto_comparacao(texto):
    texto_base = unicodedata.normalize("NFKD", (texto or "")).encode("ascii", "ignore").decode("ascii")
    texto_base = re.sub(r"\s+", " ", texto_base).strip().lower()
    return texto_base


def encontrar_exercicio_duplicado(exercicios, nome, grupo, subgrupo, aparelho, id_ignorar=""):
    nome_cmp = normalizar_texto_comparacao(nome)
    grupo_cmp = normalizar_texto_comparacao(grupo)
    subgrupo_cmp = normalizar_texto_comparacao(subgrupo)
    aparelho_cmp = normalizar_texto_comparacao(aparelho)
    id_ignorar = (id_ignorar or "").strip()

    if not nome_cmp or not grupo_cmp or not subgrupo_cmp or not aparelho_cmp:
        return None

    for exercicio in exercicios:
        if not isinstance(exercicio, dict):
            continue
        if id_ignorar and (exercicio.get("id") or "").strip() == id_ignorar:
            continue

        if (
            normalizar_texto_comparacao(exercicio.get("nome")) == nome_cmp
            and normalizar_texto_comparacao(exercicio.get("grupo")) == grupo_cmp
            and normalizar_texto_comparacao(exercicio.get("subgrupo")) == subgrupo_cmp
            and normalizar_texto_comparacao(exercicio.get("aparelho")) == aparelho_cmp
        ):
            return exercicio

    return None


def gerar_campos_exercicio_padrao(nome, grupo, subgrupo, aparelho):
    base_nome = (nome or "exercício").strip()
    base_grupo = (grupo or "grupo muscular").strip()
    base_subgrupo = (subgrupo or "subgrupo").strip()
    base_aparelho = (aparelho or "equipamento").strip()
    return {
        "dicas": [
            f"Mantenha postura estável durante todo o {base_nome.lower()}.",
            "Controle a fase de subida e a fase de retorno sem impulso.",
            f"Ajuste a carga para priorizar execução técnica no {base_aparelho.lower()}.",
        ],
        "erros": [
            "Usar carga excessiva e compensar com balanço do tronco.",
            "Reduzir amplitude e encurtar o movimento.",
            "Prender a respiração durante as repetições.",
        ],
        "observacoes": (
            f"Exercício voltado para {base_grupo.lower()} com ênfase em {base_subgrupo.lower()}. "
            "Priorize ritmo constante, amplitude segura e progressão gradual de carga."
        ),
        "foco": base_subgrupo,
        "musculos_secundarios": ["Core", "Estabilizadores da escápula"],
        "instrucoes": [
            f"Posicione-se corretamente no {base_aparelho.lower()}.",
            "Ative o abdômen e alinhe coluna e ombros.",
            "Execute a fase concêntrica de forma controlada.",
            "Retorne lentamente na fase excêntrica mantendo tensão.",
            "Repita o número de repetições planejado sem perder técnica.",
        ],
    }


def gerar_campos_exercicio_por_referencia(exercicios, nome, grupo, subgrupo, aparelho):
    candidatos = []
    grupo_cmp = normalizar_texto_comparacao(grupo)
    subgrupo_cmp = normalizar_texto_comparacao(subgrupo)
    aparelho_cmp = normalizar_texto_comparacao(aparelho)

    for ex in exercicios or []:
        if not isinstance(ex, dict):
            continue

        score = 0
        if normalizar_texto_comparacao(ex.get("grupo")) == grupo_cmp:
            score += 1
        if normalizar_texto_comparacao(ex.get("subgrupo")) == subgrupo_cmp:
            score += 2
        if normalizar_texto_comparacao(ex.get("aparelho")) == aparelho_cmp:
            score += 3

        if score > 0:
            candidatos.append((score, ex))

    candidatos.sort(key=lambda item: (-item[0], (item[1].get("nome") or "").lower()))
    referencia = candidatos[0][1] if candidatos else {}

    padrao = gerar_campos_exercicio_padrao(nome, grupo, subgrupo, aparelho)

    dicas_ref = normalizar_lista_texto(referencia.get("dicas") or [])
    erros_ref = normalizar_lista_texto(referencia.get("erros") or [])
    musculos_ref = normalizar_lista_texto(referencia.get("musculos_secundarios") or [])
    instrucoes_ref = normalizar_lista_texto(referencia.get("instrucoes") or [])
    observacoes_ref = (referencia.get("observacoes") or "").strip()

    return {
        "dicas": (dicas_ref[:5] if dicas_ref else padrao["dicas"]),
        "erros": (erros_ref[:5] if erros_ref else padrao["erros"]),
        "observacoes": observacoes_ref or padrao["observacoes"],
        "foco": (referencia.get("foco") or subgrupo or "").strip(),
        "musculos_secundarios": (musculos_ref[:5] if musculos_ref else padrao["musculos_secundarios"]),
        "instrucoes": (instrucoes_ref[:8] if instrucoes_ref else padrao["instrucoes"]),
    }


def gerar_campos_exercicio_com_ia(nome, grupo, subgrupo, aparelho, exercicios_referencia=None):
    openai_api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    gemini_api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    fallback = gerar_campos_exercicio_por_referencia(exercicios_referencia or [], nome, grupo, subgrupo, aparelho)
    provedor = ""
    if openai_api_key:
        provedor = "openai"
    elif gemini_api_key:
        provedor = "gemini"

    if not provedor:
        return (
            fallback,
            "",
            "Nenhuma chave de IA configurada (OPENAI_API_KEY ou GEMINI_API_KEY); campos sugeridos automaticamente com base na biblioteca atual.",
        )

    prompt_sistema = (
        "Você é especialista em treinamento físico e deve gerar campos padronizados para cadastro de exercício. "
        "Responda APENAS em JSON válido com as chaves exatas: "
        "dicas (array de strings), erros (array de strings), observacoes (string), foco (string), "
        "musculos_secundarios (array de strings), instrucoes (array de strings)."
    )
    prompt_usuario = (
        "Gere os campos para o exercício abaixo em português do Brasil, com linguagem objetiva e técnica.\n"
        f"Nome do exercício: {nome}\n"
        f"Grupo muscular: {grupo}\n"
        f"Subgrupo muscular: {subgrupo}\n"
        f"Aparelho/implemento: {aparelho}\n"
        "Regras:\n"
        "- Dicas: entre 3 e 5 itens curtos.\n"
        "- Erros: entre 3 e 5 itens curtos.\n"
        "- Observações: 1 parágrafo curto.\n"
        "- Foco: texto curto (pode ser vazio se realmente não se aplicar).\n"
        "- Músculos secundários: entre 2 e 5 itens.\n"
        "- Instruções: entre 4 e 8 passos objetivos."
    )

    if provedor == "openai":
        modelo_openai = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
        payload = {
            "model": modelo_openai,
            "messages": [
                {"role": "system", "content": prompt_sistema},
                {"role": "user", "content": prompt_usuario},
            ],
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {openai_api_key}",
            },
            method="POST",
        )
    else:
        modelo_gemini = (os.environ.get("GEMINI_MODEL") or "gemini-1.5-flash").strip()
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                f"{prompt_sistema}\n\n{prompt_usuario}\n\n"
                                "Retorne somente JSON válido, sem markdown."
                            )
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.4,
                "responseMimeType": "application/json",
            },
        }
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{modelo_gemini}:generateContent?key={gemini_api_key}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            _ = e.read().decode("utf-8")
        except Exception:
            pass
        return fallback, "", f"Falha ao usar IA ({provedor.upper()} - HTTP {e.code}). Sugestões automáticas foram aplicadas."
    except Exception:
        return fallback, "", f"Falha de comunicação com o serviço de IA ({provedor.upper()}); sugestões automáticas foram aplicadas."

    try:
        resposta = json.loads(body)
        conteudo = ""
        if provedor == "openai":
            conteudo = resposta["choices"][0]["message"]["content"]
            if isinstance(conteudo, list):
                conteudo = "".join(
                    parte.get("text", "") if isinstance(parte, dict) else str(parte)
                    for parte in conteudo
                )
        else:
            partes = (((resposta.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
            conteudo = "".join((parte.get("text") or "") for parte in partes if isinstance(parte, dict))

        conteudo = (conteudo or "").strip()
        if conteudo.startswith("```"):
            conteudo = re.sub(r"^```(?:json)?\s*", "", conteudo, flags=re.IGNORECASE)
            conteudo = re.sub(r"\s*```$", "", conteudo)
        dados = json.loads(conteudo)
    except (ValueError, KeyError, IndexError, TypeError):
        return fallback, "", f"A resposta da IA ({provedor.upper()}) veio em formato inválido; sugestões automáticas foram aplicadas."

    resultado = {
        "dicas": normalizar_lista_texto(dados.get("dicas") or []),
        "erros": normalizar_lista_texto(dados.get("erros") or []),
        "observacoes": (dados.get("observacoes") or "").strip(),
        "foco": (dados.get("foco") or "").strip(),
        "musculos_secundarios": normalizar_lista_texto(dados.get("musculos_secundarios") or []),
        "instrucoes": normalizar_lista_texto(dados.get("instrucoes") or []),
    }

    if not resultado["dicas"] or not resultado["erros"] or not resultado["instrucoes"]:
        return fallback, "", f"A IA ({provedor.upper()}) não retornou conteúdo suficiente; sugestões automáticas foram aplicadas."

    return resultado, "", ""


def gerar_campos_exercicio_com_ia(nome, grupo, subgrupo, aparelho):
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None, "OPENAI_API_KEY não configurada no servidor."

    modelo = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()

    prompt_sistema = (
        "Você é especialista em treinamento físico e deve gerar campos padronizados para cadastro de exercício. "
        "Responda APENAS em JSON válido com as chaves exatas: "
        "dicas (array de strings), erros (array de strings), observacoes (string), foco (string), "
        "musculos_secundarios (array de strings), instrucoes (array de strings)."
    )
    prompt_usuario = (
        "Gere os campos para o exercício abaixo em português do Brasil, com linguagem objetiva e técnica.\n"
        f"Nome do exercício: {nome}\n"
        f"Grupo muscular: {grupo}\n"
        f"Subgrupo muscular: {subgrupo}\n"
        f"Aparelho/implemento: {aparelho}\n"
        "Regras:\n"
        "- Dicas: entre 3 e 5 itens curtos.\n"
        "- Erros: entre 3 e 5 itens curtos.\n"
        "- Observações: 1 parágrafo curto.\n"
        "- Foco: texto curto (pode ser vazio se realmente não se aplicar).\n"
        "- Músculos secundários: entre 2 e 5 itens.\n"
        "- Instruções: entre 4 e 8 passos objetivos."
    )

    payload = {
        "model": modelo,
        "messages": [
            {"role": "system", "content": prompt_sistema},
            {"role": "user", "content": prompt_usuario},
        ],
        "temperature": 0.4,
        "response_format": {"type": "json_object"},
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detalhe = ""
        try:
            detalhe = e.read().decode("utf-8")
        except Exception:
            detalhe = ""
        return None, f"Falha ao gerar conteúdo com IA ({e.code}). {detalhe[:300]}".strip()
    except Exception:
        return None, "Falha de comunicação com o serviço de IA."

    try:
        resposta = json.loads(body)
        conteudo = resposta["choices"][0]["message"]["content"]
        if isinstance(conteudo, list):
            conteudo = "".join(
                parte.get("text", "") if isinstance(parte, dict) else str(parte)
                for parte in conteudo
            )
        dados = json.loads(conteudo)
    except (ValueError, KeyError, IndexError, TypeError):
        return None, "A IA retornou um formato inválido."

    resultado = {
        "dicas": normalizar_lista_texto(dados.get("dicas") or []),
        "erros": normalizar_lista_texto(dados.get("erros") or []),
        "observacoes": (dados.get("observacoes") or "").strip(),
        "foco": (dados.get("foco") or "").strip(),
        "musculos_secundarios": normalizar_lista_texto(dados.get("musculos_secundarios") or []),
        "instrucoes": normalizar_lista_texto(dados.get("instrucoes") or []),
    }

    if not resultado["dicas"] or not resultado["erros"] or not resultado["instrucoes"]:
        return None, "A IA não retornou conteúdo suficiente para preencher o exercício."

    return resultado, ""


def normalizar_chave_busca(texto):
    return normalizar_chave_codigo(texto)


def encontrar_exercicio_duplicado(exercicios, nome, grupo, subgrupo, aparelho):
    chave_nome = normalizar_chave_busca(nome)
    chave_grupo = normalizar_chave_busca(grupo)
    chave_subgrupo = normalizar_chave_busca(subgrupo)
    chave_aparelho = normalizar_chave_busca(aparelho)

    if not all([chave_nome, chave_grupo, chave_subgrupo, chave_aparelho]):
        return None

    for ex in exercicios or []:
        if not isinstance(ex, dict):
            continue
        if (
            normalizar_chave_busca(ex.get("nome")) == chave_nome
            and normalizar_chave_busca(ex.get("grupo")) == chave_grupo
            and normalizar_chave_busca(ex.get("subgrupo")) == chave_subgrupo
            and normalizar_chave_busca(ex.get("aparelho")) == chave_aparelho
        ):
            return ex
    return None


def montar_sugestao_campos_exercicio(exercicio):
    if not isinstance(exercicio, dict):
        return {}
    return {
        "dicas": "\n".join(exercicio.get("dicas") or []),
        "erros": "\n".join(exercicio.get("erros") or []),
        "observacoes": exercicio.get("observacoes") or "",
        "foco": exercicio.get("foco") or "",
        "musculos_secundarios": "\n".join(exercicio.get("musculos_secundarios") or []),
        "instrucoes": "\n".join(exercicio.get("instrucoes") or []),
    }


def buscar_referencia_autocomplete(exercicios, nome, grupo, subgrupo, aparelho):
    chave_grupo = normalizar_chave_busca(grupo)
    chave_subgrupo = normalizar_chave_busca(subgrupo)
    chave_aparelho = normalizar_chave_busca(aparelho)
    nome_ref = (nome or "").strip().lower()

    if not all([nome_ref, chave_grupo, chave_subgrupo, chave_aparelho]):
        return None

    candidatos = []
    for ex in exercicios or []:
        if not isinstance(ex, dict):
            continue

        if (
            normalizar_chave_busca(ex.get("grupo")) != chave_grupo
            or normalizar_chave_busca(ex.get("subgrupo")) != chave_subgrupo
            or normalizar_chave_busca(ex.get("aparelho")) != chave_aparelho
        ):
            continue

        nome_candidato = (ex.get("nome") or "").strip().lower()
        if not nome_candidato:
            continue

        similaridade = SequenceMatcher(a=nome_ref, b=nome_candidato).ratio()
        candidatos.append((similaridade, nome_candidato, ex))

    if not candidatos:
        return None

    candidatos.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidatos[0][2]




def extrair_json_de_texto_ia(texto):
    conteudo = (texto or "").strip()
    if not conteudo:
        return {}

    if conteudo.startswith("```"):
        partes = conteudo.split("```")
        for parte in partes:
            bloco = parte.strip()
            if not bloco:
                continue
            if bloco.lower().startswith("json"):
                bloco = bloco[4:].strip()
            try:
                dado = json.loads(bloco)
                if isinstance(dado, dict):
                    return dado
            except json.JSONDecodeError:
                continue

    try:
        dado = json.loads(conteudo)
        if isinstance(dado, dict):
            return dado
    except json.JSONDecodeError:
        pass

    inicio = conteudo.find("{")
    fim = conteudo.rfind("}")
    if inicio >= 0 and fim > inicio:
        trecho = conteudo[inicio:fim + 1]
        try:
            dado = json.loads(trecho)
            if isinstance(dado, dict):
                return dado
        except json.JSONDecodeError:
            return {}

    return {}


def normalizar_campo_ia_lista(valor):
    itens = []

    if isinstance(valor, list):
        itens = [str(v).strip() for v in valor if str(v).strip()]
    else:
        texto = str(valor or "").replace("\r", "").strip()
        if not texto:
            return ""

        texto = texto.replace("\\n", "\n")
        linhas = [linha.strip() for linha in texto.split("\n") if linha.strip()]
        if len(linhas) <= 1:
            linhas = [
                pedaco.strip(" -•	")
                for pedaco in re.split(r"[;|]", texto)
                if pedaco.strip(" -•	")
            ]
        itens = [
            re.sub(r"^\d+[.)-]?\s*", "", linha).strip(" -•	")
            for linha in linhas
            if linha.strip(" -•	")
        ]

    itens_unicos = []
    vistos = set()
    for item in itens:
        chave = normalizar_chave_busca(item)
        if not chave or chave in vistos:
            continue
        vistos.add(chave)
        itens_unicos.append(item)

    return "\n".join(itens_unicos)


def normalizar_campo_ia_texto(valor):
    if isinstance(valor, list):
        return "\n".join(str(v).strip() for v in valor if str(v).strip())
    return str(valor or "").replace("\\n", "\n").strip()


def foco_ia_parece_generico(foco):
    texto = normalizar_chave_busca(foco)
    if not texto:
        return True

    termos_genericos = [
        "seguranca",
        "execucao tecnica",
        "execucao segura",
        "beneficio geral",
        "condicionamento",
    ]
    return any(termo in texto for termo in termos_genericos)


def gerar_campos_com_ia(nome, nome_original, grupo, subgrupo, aparelho):
    api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not api_key:
        return {}, "IA não configurada. Defina GROQ_API_KEY no ambiente."

    modelo_configurado = (os.environ.get("GROQ_MODEL") or "llama-3.3-70b-versatile").strip()
    modelos_tentativa = [modelo_configurado]
    for candidato in ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]:
        if candidato not in modelos_tentativa:
            modelos_tentativa.append(candidato)

    prompt_sistema = (
        "Você é especialista em educação física, biomecânica e prescrição de exercícios. "
        "Responda SOMENTE JSON válido, sem markdown, com as chaves: "
        "dicas (array), erros (array), observacoes (string), foco (string), "
        "musculos_secundarios (array), instrucoes (array). "
        "Todo o conteúdo em Português do Brasil. "
        "NÃO use orientações genéricas nem frases vagas."
    )

    prompt_usuario = (
        "Gere conteúdo para cadastro de exercício com foco em qualidade técnica.\n"
        f"Nome em português: {nome}\n"
        f"Nome original em inglês: {nome_original}\n"
        f"Grupo: {grupo}\n"
        f"Subgrupo: {subgrupo}\n"
        f"Aparelho: {aparelho}\n"
        "Regras obrigatórias:\n"
        "1) dicas: 4 a 6 itens curtos e específicos para esse exercício.\n"
        "2) erros: 4 a 6 itens reais e específicos para esse exercício.\n"
        "3) observacoes: texto obrigatório com 2 a 4 frases, incluindo progressão/regressão quando fizer sentido.\n"
        "4) foco: frase curta de objetivo biomecânico/fisiológico (evite 'Segurança e Execução Técnica').\n"
        "5) musculos_secundarios: 3 a 6 músculos/grupos anatômicos plausíveis.\n"
        "6) instrucoes: 4 a 8 passos de execução do movimento informado, sem inventar outro exercício."
    )

    resposta = None
    ultimo_erro = ""
    for modelo in modelos_tentativa:
        try:
            client = Groq(api_key=api_key)
            resposta = client.chat.completions.create(
                model=modelo,
                temperature=0.3,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": prompt_sistema},
                    {"role": "user", "content": prompt_usuario},
                ],
            )
            break
        except Exception as e:
            ultimo_erro = str(e)
            erro_txt = ultimo_erro.lower()
            modelo_invalido = "decommission" in erro_txt or "invalid_request_error" in erro_txt or "model" in erro_txt
            if not modelo_invalido:
                return {}, f"Falha ao consultar IA da Groq. {ultimo_erro[:300]}"

    if not resposta:
        return {}, f"Falha ao consultar IA da Groq. {ultimo_erro[:300]}"

    conteudo = ""
    if resposta and resposta.choices:
        conteudo = (resposta.choices[0].message.content or "").strip()

    estrutura = extrair_json_de_texto_ia(conteudo)
    if not estrutura:
        return {}, "A IA não retornou JSON válido para os campos solicitados."

    if isinstance(estrutura.get("campos"), dict):
        estrutura = estrutura.get("campos") or {}

    campos = {
        "dicas": normalizar_campo_ia_lista(estrutura.get("dicas")),
        "erros": normalizar_campo_ia_lista(estrutura.get("erros")),
        "observacoes": normalizar_campo_ia_texto(estrutura.get("observacoes")),
        "foco": normalizar_campo_ia_texto(estrutura.get("foco")),
        "musculos_secundarios": normalizar_campo_ia_lista(estrutura.get("musculos_secundarios")),
        "instrucoes": normalizar_campo_ia_lista(estrutura.get("instrucoes")),
    }

    if not campos["observacoes"]:
        campos["observacoes"] = (
            f"{nome} exige controle de tronco e execução consciente. "
            "Ajuste amplitude e tempo de contração conforme seu nível para manter técnica e estabilidade."
        )

    if foco_ia_parece_generico(campos["foco"]):
        subgrupo_base = (subgrupo or "movimento").strip().lower() or "movimento"
        campos["foco"] = f"Melhora do controle do {subgrupo_base} com estabilidade do core"

    if not any(campos.values()):
        return {}, "A IA retornou vazio para os campos sugeridos."

    return campos, ""

def listar_pastas_exercicios():
    if not os.path.isdir(EXERCICIOS_STATIC_DIR):
        return []

    pastas = []
    for nome in os.listdir(EXERCICIOS_STATIC_DIR):
        caminho = os.path.join(EXERCICIOS_STATIC_DIR, nome)
        if os.path.isdir(caminho):
            pastas.append(nome)
    return sorted(pastas)


def carregar_categorias_exercicios(exercicios):
    grupos_padrao = obter_grupos_validos(exercicios)
    subgrupos_padrao = obter_subgrupos_por_grupo(exercicios)
    aparelhos_padrao = obter_aparelhos_por_filtros(exercicios)["todos"]

    data = {}
    if os.path.exists(EXERCICIOS_CATEGORIAS_FILE):
        try:
            with open(EXERCICIOS_CATEGORIAS_FILE, "r", encoding="utf-8") as f:
                carregado = json.load(f)
                if isinstance(carregado, dict):
                    data = carregado
        except (json.JSONDecodeError, OSError):
            data = {}

    grupos_extra = normalizar_lista_texto(data.get("grupos"))
    aparelhos_extra = normalizar_lista_texto(data.get("aparelhos"))
    subgrupos_extra = {}
    bruto_subgrupos = data.get("subgrupos_por_grupo")
    if isinstance(bruto_subgrupos, dict):
        for grupo, lista in bruto_subgrupos.items():
            grupo_txt = (grupo or "").strip()
            if not grupo_txt:
                continue
            subgrupos_extra[grupo_txt] = normalizar_lista_texto(lista)

    grupos = normalizar_lista_texto(grupos_padrao + grupos_extra + list(subgrupos_extra.keys()))
    grupos.sort(key=lambda x: x.lower())

    subgrupos_por_grupo = {}
    for grupo in grupos:
        chave_lower = grupo.lower()
        padrao = subgrupos_padrao.get(chave_lower, [])
        extra = []
        for k, v in subgrupos_extra.items():
            if k.lower() == chave_lower:
                extra = v
                break
        subgrupos_por_grupo[grupo] = normalizar_lista_texto(padrao + extra)
        subgrupos_por_grupo[grupo].sort(key=lambda x: x.lower())

    aparelhos = normalizar_lista_texto(aparelhos_padrao + aparelhos_extra)
    aparelhos.sort(key=lambda x: x.lower())

    vinculos_aparelho = []
    bruto_vinculos = data.get("vinculos_aparelho")
    if isinstance(bruto_vinculos, list):
        vistos = set()
        for item in bruto_vinculos:
            if not isinstance(item, dict):
                continue
            grupo = (item.get("grupo") or "").strip()
            subgrupo = (item.get("subgrupo") or "").strip()
            aparelho = (item.get("aparelho") or "").strip()
            if not grupo or not subgrupo or not aparelho:
                continue
            chave = f"{grupo.lower()}|||{subgrupo.lower()}|||{aparelho.lower()}"
            if chave in vistos:
                continue
            vistos.add(chave)
            vinculos_aparelho.append(
                {
                    "grupo": grupo,
                    "subgrupo": subgrupo,
                    "aparelho": aparelho,
                }
            )
    vinculos_aparelho.sort(key=lambda x: ((x.get("grupo") or "").lower(), (x.get("subgrupo") or "").lower(), (x.get("aparelho") or "").lower()))

    return {
        "grupos": grupos,
        "subgrupos_por_grupo": subgrupos_por_grupo,
        "aparelhos": aparelhos,
        "vinculos_aparelho": vinculos_aparelho,
    }


def salvar_categorias_exercicios(categorias):
    with open(EXERCICIOS_CATEGORIAS_FILE, "w", encoding="utf-8") as f:
        json.dump(categorias, f, ensure_ascii=False, indent=2)


def normalizar_categorias_para_salvar(categorias):
    grupos = normalizar_lista_texto(categorias.get("grupos"))
    grupos.sort(key=lambda x: x.lower())

    subgrupos_por_grupo = {}
    for grupo in grupos:
        lista = normalizar_lista_texto((categorias.get("subgrupos_por_grupo") or {}).get(grupo, []))
        lista.sort(key=lambda x: x.lower())
        subgrupos_por_grupo[grupo] = lista

    aparelhos = normalizar_lista_texto(categorias.get("aparelhos"))
    aparelhos.sort(key=lambda x: x.lower())

    grupos_por_lower = {g.lower(): g for g in grupos}
    aparelhos_por_lower = {a.lower(): a for a in aparelhos}
    vinculos_aparelho = []
    vistos = set()
    for item in categorias.get("vinculos_aparelho") or []:
        if not isinstance(item, dict):
            continue
        grupo_bruto = (item.get("grupo") or "").strip()
        subgrupo_bruto = (item.get("subgrupo") or "").strip()
        aparelho_bruto = (item.get("aparelho") or "").strip()
        if not grupo_bruto or not subgrupo_bruto or not aparelho_bruto:
            continue

        grupo = grupos_por_lower.get(grupo_bruto.lower())
        if not grupo:
            continue

        subgrupos_grupo = subgrupos_por_grupo.get(grupo, [])
        subgrupo = next((s for s in subgrupos_grupo if s.lower() == subgrupo_bruto.lower()), "")
        if not subgrupo:
            continue

        aparelho = aparelhos_por_lower.get(aparelho_bruto.lower())
        if not aparelho:
            continue

        chave = f"{grupo.lower()}|||{subgrupo.lower()}|||{aparelho.lower()}"
        if chave in vistos:
            continue
        vistos.add(chave)
        vinculos_aparelho.append({"grupo": grupo, "subgrupo": subgrupo, "aparelho": aparelho})

    vinculos_aparelho.sort(key=lambda x: (x["grupo"].lower(), x["subgrupo"].lower(), x["aparelho"].lower()))

    return {
        "grupos": grupos,
        "subgrupos_por_grupo": subgrupos_por_grupo,
        "aparelhos": aparelhos,
        "vinculos_aparelho": vinculos_aparelho,
    }


def obter_aparelhos_por_filtros_com_vinculos(exercicios, vinculos_aparelho):
    base = obter_aparelhos_por_filtros(exercicios)
    todos = set(base.get("todos") or [])
    por_grupo = {k: set(v or []) for k, v in (base.get("por_grupo") or {}).items()}
    por_subgrupo = {k: set(v or []) for k, v in (base.get("por_subgrupo") or {}).items()}
    por_grupo_subgrupo = {k: set(v or []) for k, v in (base.get("por_grupo_subgrupo") or {}).items()}

    for item in vinculos_aparelho or []:
        if not isinstance(item, dict):
            continue
        grupo = (item.get("grupo") or "").strip().lower()
        subgrupo = (item.get("subgrupo") or "").strip().lower()
        aparelho = (item.get("aparelho") or "").strip()
        if not grupo or not subgrupo or not aparelho:
            continue

        todos.add(aparelho)
        por_grupo.setdefault(grupo, set()).add(aparelho)
        por_subgrupo.setdefault(subgrupo, set()).add(aparelho)
        por_grupo_subgrupo.setdefault(f"{grupo}|||{subgrupo}", set()).add(aparelho)

    return {
        "todos": sorted(todos),
        "por_grupo": {k: sorted(v) for k, v in por_grupo.items()},
        "por_subgrupo": {k: sorted(v) for k, v in por_subgrupo.items()},
        "por_grupo_subgrupo": {k: sorted(v) for k, v in por_grupo_subgrupo.items()},
    }




def normalizar_chave_codigo(texto):
    base = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(base.lower().strip().split())


def obter_codigo_grupo(grupo):
    return GRUPOS_CODIGOS_PADRAO.get(normalizar_chave_codigo(grupo))


def construir_codigos_segmento(exercicios, campo, inicio, fim, referencias=None):
    codigos = {}
    usados = set()

    for ex in exercicios or []:
        if not isinstance(ex, dict):
            continue

        ex_id = str(ex.get("id") or "").strip()
        nome = (ex.get(campo) or "").strip()
        if not nome:
            continue

        chave_nome = nome.lower()
        if PADRAO_ID_EXERCICIO.fullmatch(ex_id):
            codigo = ex_id[inicio:fim]
            if codigo.isdigit() and 1 <= int(codigo) <= 99:
                codigos.setdefault(chave_nome, codigo)
                usados.add(codigo)

    referencias_validas = sorted({(r or "").strip() for r in (referencias or []) if (r or "").strip()}, key=lambda x: x.lower())
    for nome in referencias_validas:
        chave_nome = nome.lower()
        if chave_nome in codigos:
            continue

        for n in range(1, 100):
            codigo = f"{n:02d}"
            if codigo in usados:
                continue
            codigos[chave_nome] = codigo
            usados.add(codigo)
            break

    return codigos


def gerar_id_exercicio_por_categorias(exercicios, grupo, subgrupo, aparelho, id_exercicio_atual=None, subgrupos_referencia=None, aparelhos_referencia=None):
    codigo_grupo = obter_codigo_grupo(grupo)
    if not codigo_grupo:
        return "", "Grupo inválido para composição automática do ID."

    mapa_subgrupos = construir_codigos_segmento(
        exercicios,
        campo="subgrupo",
        inicio=1,
        fim=3,
        referencias=subgrupos_referencia,
    )
    mapa_aparelhos = construir_codigos_segmento(
        exercicios,
        campo="aparelho",
        inicio=3,
        fim=5,
        referencias=aparelhos_referencia,
    )

    codigo_subgrupo = mapa_subgrupos.get((subgrupo or "").strip().lower())
    if not codigo_subgrupo:
        return "", "Subgrupo inválido para composição automática do ID."

    codigo_aparelho = mapa_aparelhos.get((aparelho or "").strip().lower())
    if not codigo_aparelho:
        return "", "Aparelho inválido para composição automática do ID."

    prefixo = f"{codigo_grupo}{codigo_subgrupo}{codigo_aparelho}"
    em_uso = set()
    atual = str(id_exercicio_atual or "").strip()

    for ex in exercicios or []:
        if not isinstance(ex, dict):
            continue
        ex_id = str(ex.get("id") or "").strip()
        if not PADRAO_ID_EXERCICIO.fullmatch(ex_id):
            continue
        if atual and ex_id == atual:
            continue
        if not ex_id.startswith(prefixo):
            continue
        em_uso.add(int(ex_id[-4:]))

    for sequencial in range(1, 10000):
        if sequencial in em_uso:
            continue
        return f"{prefixo}{sequencial:04d}", ""

    return "", f"Não há sequenciais disponíveis para o prefixo {prefixo}."


def normalizar_data_iso(valor):
    """Normaliza data para formato YYYY-MM-DD quando possível."""
    if not valor:
        return ""

    texto = str(valor).strip()
    if not texto:
        return ""

    # já no formato esperado
    if len(texto) >= 10 and texto[4] == "-" and texto[7] == "-":
        candidato = texto[:10]
        try:
            datetime.strptime(candidato, "%Y-%m-%d")
            return candidato
        except ValueError:
            pass

    for fmt in ("%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return ""






def normalizar_lista_texto(valor):
    """Normaliza valor textual para lista sem duplicatas."""
    if isinstance(valor, list):
        candidatos = valor
    elif isinstance(valor, str):
        candidatos = [parte.strip() for parte in valor.split(",")]
    else:
        candidatos = []

    itens = []
    vistos = set()
    for item in candidatos:
        texto = str(item).strip()
        if not texto:
            continue
        chave = texto.lower()
        if chave in vistos:
            continue
        vistos.add(chave)
        itens.append(texto)
    return itens


def exibir_lista(lista, fallback="-"):
    lista = normalizar_lista_texto(lista)
    return ", ".join(lista) if lista else fallback




def obter_grupos_validos(exercicios):
    """Extrai grupos únicos de forma defensiva."""
    if not isinstance(exercicios, list):
        return []

    grupos = []
    vistos = set()
    for ex in exercicios:
        if not isinstance(ex, dict):
            continue

        grupo = (ex.get("grupo") or "").strip()
        if not grupo or grupo in vistos:
            continue

        vistos.add(grupo)
        grupos.append(grupo)

    return sorted(grupos)


def obter_subgrupos_validos(exercicios):
    """Extrai subgrupos únicos de forma defensiva."""
    if not isinstance(exercicios, list):
        return []

    subgrupos = []
    vistos = set()
    for ex in exercicios:
        if not isinstance(ex, dict):
            continue

        subgrupo = (ex.get("subgrupo") or "").strip()
        if not subgrupo or subgrupo in vistos:
            continue

        vistos.add(subgrupo)
        subgrupos.append(subgrupo)

    return sorted(subgrupos)




def obter_subgrupos_por_grupo(exercicios):
    """Mapeia grupo (lowercase) -> lista de subgrupos do grupo."""
    if not isinstance(exercicios, list):
        return {}

    mapeamento = {}
    for ex in exercicios:
        if not isinstance(ex, dict):
            continue

        grupo = (ex.get("grupo") or "").strip()
        subgrupo = (ex.get("subgrupo") or "").strip()
        if not grupo or not subgrupo:
            continue

        chave = grupo.lower()
        mapeamento.setdefault(chave, set()).add(subgrupo)

    return {chave: sorted(subgrupos) for chave, subgrupos in mapeamento.items()}


def obter_aparelhos_por_filtros(exercicios):
    """Mapeia aparelhos por grupo/subgrupo para filtros dinâmicos."""
    if not isinstance(exercicios, list):
        return {
            "todos": [],
            "por_grupo": {},
            "por_subgrupo": {},
            "por_grupo_subgrupo": {},
        }

    todos = set()
    por_grupo = {}
    por_subgrupo = {}
    por_grupo_subgrupo = {}

    for ex in exercicios:
        if not isinstance(ex, dict):
            continue

        aparelho = (ex.get("aparelho") or "").strip()
        if not aparelho:
            continue

        grupo = (ex.get("grupo") or "").strip().lower()
        subgrupo = (ex.get("subgrupo") or "").strip().lower()
        todos.add(aparelho)

        if grupo:
            por_grupo.setdefault(grupo, set()).add(aparelho)
        if subgrupo:
            por_subgrupo.setdefault(subgrupo, set()).add(aparelho)
        if grupo and subgrupo:
            chave = f"{grupo}|||{subgrupo}"
            por_grupo_subgrupo.setdefault(chave, set()).add(aparelho)

    return {
        "todos": sorted(todos),
        "por_grupo": {chave: sorted(aparelhos) for chave, aparelhos in por_grupo.items()},
        "por_subgrupo": {chave: sorted(aparelhos) for chave, aparelhos in por_subgrupo.items()},
        "por_grupo_subgrupo": {
            chave: sorted(aparelhos) for chave, aparelhos in por_grupo_subgrupo.items()
        },
    }


def index_por_id(exercicios):
    """Mapa id -> exercício."""
    return {ex["id"]: ex for ex in exercicios if ex.get("id")}


def carregar_treinos():
    if db:
        try:
            registros = TreinoDB.query.all()
            treinos = [registro.dados for registro in registros if isinstance(registro.dados, dict)]
            return treinos
        except SQLAlchemyError:
            pass
        except SQLAlchemyError as e:
            app.logger.exception(f"Falha ao carregar treinos no banco: {e}")
            return []
    treinos_file = caminho_arquivo("TREINOS_FILE", "treinos.json")
    if not os.path.exists(treinos_file):
        return []
    
    try:
        with open(treinos_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    return data if isinstance(data, list) else []


def salvar_treinos(treinos):
    if db:
        treinos_validos = [normalizar_item_treino(t) for t in treinos if isinstance(t, dict)]
        ids_recebidos = {t.get("id") for t in treinos_validos if t.get("id")}
        try:
            existentes = {registro.id: registro for registro in TreinoDB.query.all()}

            for treino in treinos_validos:
                treino_id = treino.get("id")
                if not treino_id:
                    continue

                registro = existentes.get(treino_id)
                if not registro:
                    registro = TreinoDB(id=treino_id)

                registro.link_id = (treino.get("link_id") or treino_id).strip()
                registro.dados = treino
                db.session.add(registro)

            for treino_id, registro in existentes.items():
                if treino_id not in ids_recebidos:
                    db.session.delete(registro)

            db.session.commit()
            return
        except SQLAlchemyError as e:
            db.session.rollback()
            app.logger.exception(f"Falha ao salvar treinos no banco: {e}")
            raise
    treinos_file = caminho_arquivo("TREINOS_FILE", "treinos.json")
    with open(treinos_file, "w", encoding="utf-8") as f:
        json.dump(treinos, f, ensure_ascii=False, indent=2)


def carregar_respostas_questionario():
    if db:
        try:
            registros = RespostaDB.query.all()
            respostas = [registro.dados for registro in registros if isinstance(registro.dados, dict)]
            return respostas
        except SQLAlchemyError as e:
            app.logger.exception(f"Falha ao carregar respostas no banco: {e}")
            return []
    questionario_file = caminho_arquivo("QUESTIONARIO_FILE", "questionario_respostas.json")
    if not os.path.exists(questionario_file):
        return []

    try:
        with open(questionario_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    return data if isinstance(data, list) else []


def salvar_respostas_questionario(respostas):
    if db:
        respostas_validas = [r for r in respostas if isinstance(r, dict) and r.get("id")]
        ids_recebidos = {r["id"] for r in respostas_validas}
        try:
            existentes = {registro.id: registro for registro in RespostaDB.query.all()}

            for resposta in respostas_validas:
                registro = existentes.get(resposta["id"])
                if not registro:
                    registro = RespostaDB(id=resposta["id"])
                registro.dados = resposta
                db.session.add(registro)

            for resposta_id, registro in existentes.items():
                if resposta_id not in ids_recebidos:
                    db.session.delete(registro)

            db.session.commit()
            return
        except SQLAlchemyError as e:
            db.session.rollback()
            app.logger.exception(f"Falha ao salvar respostas no banco: {e}")
            raise
    questionario_file = caminho_arquivo("QUESTIONARIO_FILE", "questionario_respostas.json")
    with open(questionario_file, "w", encoding="utf-8") as f:
        json.dump(respostas, f, ensure_ascii=False, indent=2)


def limpar_texto_campo(valor):
    return (str(valor or "")).strip()


def normalizar_item_treino(item):
    if not isinstance(item, dict):
        item = {}
    item.setdefault("id", "")
    item.setdefault("link_id", "")
    item.setdefault("aluno", "")
    item.setdefault("objetivo", "")

    tipos = normalizar_lista_texto(item.get("tipos") or item.get("tipo"))
    grupos = normalizar_lista_texto(item.get("grupos") or item.get("grupo"))

    # Compatibilidade com estrutura antiga (tipo/grupo string)
    item["tipos"] = tipos
    item["grupos"] = grupos
    item["tipo"] = exibir_lista(tipos, fallback="")
    item["grupo"] = exibir_lista(grupos, fallback="")
    exercicios = item.get("exercicios")

    item["exercicios"] = exercicios if isinstance(exercicios, list) else []
    item.setdefault("criado_em", "")
    data_treino = normalizar_data_iso(item.get("data_treino"))
    if not data_treino:
        data_treino = normalizar_data_iso(item.get("criado_em"))
    item["data_treino"] = data_treino

   # Link público permanente: mantém compatibilidade com links antigos
    # e evita quebra quando o treino for editado no futuro.
    if not item.get("link_id"):
        item["link_id"] = item.get("id") or uuid.uuid4().hex[:8]
    if not item.get("public_token"):
        item["public_token"] = uuid.uuid4().hex[:12]

    aliases = normalizar_lista_texto(item.get("link_aliases"))
    if item["id"] and item["id"] != item["link_id"]:
        aliases = normalizar_lista_texto(aliases + [item["id"]])
    item["link_aliases"] = aliases
    return item


def treinos_por_id(treinos):
    return {t["id"]: t for t in treinos if t.get("id")}


def gerar_whatsapp_link_treino(treino_id, treino):
    destino = url_for("visualizar_treino", treino_id=treino_id, _external=True)
    mensagem = (
        f"Treino do aluno {treino.get('aluno') or 'Sem nome'}\n"
        f"Tipo(s): {exibir_lista(treino.get('tipos') or treino.get('tipo'))}\n"
        f"Objetivo: {treino.get('objetivo') or 'Não informado'}\n"
        f"Músculos: {exibir_lista(treino.get('grupos') or treino.get('grupo'), fallback='Não informado')}\n"
        f"Data: {normalizar_data_iso(treino.get('data_treino') or treino.get('criado_em')) or '-'}\n"
        f"Link para visualizar: {destino}"
    )
    return f"https://wa.me/?text={quote(mensagem)}"


def encontrar_treino_por_link(treinos, treino_id_ou_link):
    chave = (treino_id_ou_link or "").strip()
    if not chave:
        return None

    for treino in treinos:
        if treino.get("id") == chave:
            return treino
        if treino.get("link_id") == chave:
            return treino
        if chave in (treino.get("link_aliases") or []):
            return treino
    return None


@app.route("/")
def index():
    exercicios = carregar_exercicios()
    favoritos_admin = carregar_favoritos_admin() if admin_ativo() else set()

    q = (request.args.get("q") or "").strip().lower()
    grupo = (request.args.get("grupo") or "").strip().lower()
    subgrupo = (request.args.get("subgrupo") or "").strip().lower()
    aparelho = (request.args.get("aparelho") or "").strip().lower()
    somente_favoritos = admin_ativo() and (request.args.get("favoritos") or "") == "1"

    # Lista de grupos/aparelhos (para dropdown)
    # Mantém `grupos` sempre definido para evitar NameError no render
    # mesmo com dados malformados em `exercicios`.
    grupos = obter_grupos_validos(exercicios)
    subgrupos = obter_subgrupos_validos(exercicios)
    subgrupos_por_grupo = obter_subgrupos_por_grupo(exercicios)
    aparelhos_por_filtros = obter_aparelhos_por_filtros(exercicios)
    aparelhos = aparelhos_por_filtros["todos"]

    filtrados = []
    for ex in exercicios:
        nome = (ex.get("nome") or "").lower()
        g = (ex.get("grupo") or "").lower()
        sg = (ex.get("subgrupo") or "").lower()

        a = (ex.get("aparelho") or "").lower()

        if grupo and g != grupo:
            continue
        if subgrupo and sg != subgrupo:
            continue
        if aparelho and a != aparelho:
            continue
        if q:
            # Busca simples por nome + grupo + aparelho
            if q not in nome and q not in g and q not in sg and q not in a:
                continue
        if somente_favoritos and ex.get("id") not in favoritos_admin:
            continue

        filtrados.append(ex)

    # Ordena favoritos primeiro para admin e depois por grupo/nome
    filtrados.sort(
        key=lambda x: (
            0 if x.get("id") in favoritos_admin else 1,
            (x.get("grupo") or ""),
            (x.get("nome") or ""),
        )
    )

    return render_template(
        "index.html",
        exercicios=filtrados,
        grupos=grupos,
        subgrupos=subgrupos,
        subgrupos_por_grupo=subgrupos_por_grupo,
        aparelhos_por_filtros=aparelhos_por_filtros,
        aparelhos=aparelhos,
        q=request.args.get("q") or "",
        grupo_selecionado=request.args.get("grupo") or "",
        subgrupo_selecionado=request.args.get("subgrupo") or "",
        aparelho_selecionado=request.args.get("aparelho") or "",
        favoritos_admin=favoritos_admin,
        somente_favoritos=somente_favoritos,
    )   


@app.route("/admin/favoritos/<ex_id>/alternar", methods=["POST"])
def alternar_favorito_admin(ex_id):
    bloqueio = exigir_admin_ou_redirect()
    if bloqueio:
        return bloqueio

    exercicios = carregar_exercicios()
    mapa_exercicios = index_por_id(exercicios)
    if ex_id not in mapa_exercicios:
        abort(404)

    favoritos = carregar_favoritos_admin()
    if ex_id in favoritos:
        favoritos.remove(ex_id)
    else:
        favoritos.add(ex_id)

    salvar_favoritos_admin(favoritos)

    destino = (request.form.get("next") or request.referrer or url_for("index")).strip()
    if not destino.startswith("/"):
        destino = url_for("index")
    return redirect(destino)


@app.route("/admin/exercicios/categorias", methods=["GET", "POST"])
def admin_categorias_exercicios():
    bloqueio = exigir_admin_ou_redirect()
    if bloqueio:
        return bloqueio

    exercicios = carregar_exercicios()
    categorias = carregar_categorias_exercicios(exercicios)
    erro = ""
    sucesso = ""

    if request.method == "POST":
        acao = (request.form.get("acao") or "").strip()
        valor = (request.form.get("valor") or "").strip()
        novo_valor = (request.form.get("novo_valor") or "").strip()
        grupo = (request.form.get("grupo") or "").strip()
        subgrupo = (request.form.get("subgrupo") or "").strip()

        grupos = categorias["grupos"]
        subgrupos_por_grupo = categorias["subgrupos_por_grupo"]
        aparelhos = categorias["aparelhos"]
        vinculos_aparelho = categorias.get("vinculos_aparelho") or []

        if acao == "grupo_adicionar":
            if not valor:
                erro = "Informe o nome do grupo."
            elif any(g.lower() == valor.lower() for g in grupos):
                erro = "Este grupo já existe."
            else:
                grupos.append(valor)
                subgrupos_por_grupo.setdefault(valor, [])
                sucesso = "Grupo adicionado com sucesso."

        elif acao == "grupo_editar":
            if not valor or not novo_valor:
                erro = "Selecione o grupo e informe o novo nome."
            elif any(g.lower() == novo_valor.lower() and g.lower() != valor.lower() for g in grupos):
                erro = "Já existe outro grupo com este nome."
            else:
                grupos = [novo_valor if g.lower() == valor.lower() else g for g in grupos]
                subgrupos_antigos = []
                for chave, lista in list(subgrupos_por_grupo.items()):
                    if chave.lower() == valor.lower():
                        subgrupos_antigos = lista
                        del subgrupos_por_grupo[chave]
                subgrupos_por_grupo[novo_valor] = subgrupos_antigos
                for item in vinculos_aparelho:
                    if (item.get("grupo") or "").strip().lower() == valor.lower():
                        item["grupo"] = novo_valor

                for ex in exercicios:
                    if (ex.get("grupo") or "").strip().lower() == valor.lower():
                        ex["grupo"] = novo_valor
                salvar_exercicios(exercicios)
                sucesso = "Grupo atualizado com sucesso."

        elif acao == "grupo_excluir":
            if not valor:
                erro = "Selecione o grupo para excluir."
            else:
                grupos = [g for g in grupos if g.lower() != valor.lower()]
                for chave in list(subgrupos_por_grupo.keys()):
                    if chave.lower() == valor.lower():
                        del subgrupos_por_grupo[chave]
                vinculos_aparelho = [
                    item for item in vinculos_aparelho
                    if (item.get("grupo") or "").strip().lower() != valor.lower()
                ]
                for ex in exercicios:
                    if (ex.get("grupo") or "").strip().lower() == valor.lower():
                        ex["grupo"] = "Sem grupo"
                        ex["subgrupo"] = "Sem subgrupo"
                salvar_exercicios(exercicios)
                sucesso = "Grupo excluído com sucesso."

        elif acao == "subgrupo_adicionar":
            if not grupo or not valor:
                erro = "Selecione o grupo e informe o subgrupo."
            else:
                destino = next((g for g in grupos if g.lower() == grupo.lower()), "")
                if not destino:
                    erro = "Grupo inválido."
                else:
                    lista = subgrupos_por_grupo.setdefault(destino, [])
                    if any(s.lower() == valor.lower() for s in lista):
                        erro = "Este subgrupo já existe neste grupo."
                    else:
                        lista.append(valor)
                        sucesso = "Subgrupo adicionado com sucesso."

        elif acao == "subgrupo_editar":
            if not grupo or not valor or not novo_valor:
                erro = "Selecione grupo/subgrupo e informe o novo nome."
            else:
                destino = next((g for g in grupos if g.lower() == grupo.lower()), "")
                if not destino:
                    erro = "Grupo inválido."
                else:
                    lista = subgrupos_por_grupo.setdefault(destino, [])
                    if any(s.lower() == novo_valor.lower() and s.lower() != valor.lower() for s in lista):
                        erro = "Já existe outro subgrupo com este nome nesse grupo."
                    else:
                        subgrupos_por_grupo[destino] = [novo_valor if s.lower() == valor.lower() else s for s in lista]
                        for item in vinculos_aparelho:
                            if (item.get("grupo") or "").strip().lower() == destino.lower() and (item.get("subgrupo") or "").strip().lower() == valor.lower():
                                item["subgrupo"] = novo_valor
                        for ex in exercicios:
                            if (ex.get("grupo") or "").strip().lower() == destino.lower() and (ex.get("subgrupo") or "").strip().lower() == valor.lower():
                                ex["subgrupo"] = novo_valor
                        salvar_exercicios(exercicios)
                        sucesso = "Subgrupo atualizado com sucesso."

        elif acao == "subgrupo_excluir":
            if not grupo or not valor:
                erro = "Selecione grupo e subgrupo para excluir."
            else:
                destino = next((g for g in grupos if g.lower() == grupo.lower()), "")
                if not destino:
                    erro = "Grupo inválido."
                else:
                    subgrupos_por_grupo[destino] = [s for s in subgrupos_por_grupo.get(destino, []) if s.lower() != valor.lower()]
                    vinculos_aparelho = [
                        item for item in vinculos_aparelho
                        if not (
                            (item.get("grupo") or "").strip().lower() == destino.lower()
                            and (item.get("subgrupo") or "").strip().lower() == valor.lower()
                        )
                    ]
                    for ex in exercicios:
                        if (ex.get("grupo") or "").strip().lower() == destino.lower() and (ex.get("subgrupo") or "").strip().lower() == valor.lower():
                            ex["subgrupo"] = "Sem subgrupo"
                    salvar_exercicios(exercicios)
                    sucesso = "Subgrupo excluído com sucesso."

        elif acao == "subgrupo_mover":
            grupo_origem = (request.form.get("grupo_origem") or "").strip()
            grupo_destino = (request.form.get("grupo_destino") or "").strip()
            if not grupo_origem or not grupo_destino or not valor:
                erro = "Selecione grupo de origem, subgrupo e grupo de destino."
            else:
                origem_ref = next((g for g in grupos if g.lower() == grupo_origem.lower()), "")
                destino_ref = next((g for g in grupos if g.lower() == grupo_destino.lower()), "")
                if not origem_ref or not destino_ref:
                    erro = "Grupo de origem/destino inválido."
                elif origem_ref.lower() == destino_ref.lower():
                    erro = "Grupo de origem e destino devem ser diferentes."
                else:
                    lista_origem = subgrupos_por_grupo.setdefault(origem_ref, [])
                    subgrupo_ref = next((s for s in lista_origem if s.lower() == valor.lower()), "")
                    if not subgrupo_ref:
                        erro = "Subgrupo inválido para o grupo de origem."
                    elif any(s.lower() == subgrupo_ref.lower() for s in subgrupos_por_grupo.setdefault(destino_ref, [])):
                        erro = "O grupo de destino já possui esse subgrupo."
                    else:
                        subgrupos_por_grupo[origem_ref] = [s for s in lista_origem if s.lower() != subgrupo_ref.lower()]
                        subgrupos_por_grupo.setdefault(destino_ref, []).append(subgrupo_ref)

                        for item in vinculos_aparelho:
                            if (item.get("grupo") or "").strip().lower() == origem_ref.lower() and (item.get("subgrupo") or "").strip().lower() == subgrupo_ref.lower():
                                item["grupo"] = destino_ref

                        for ex in exercicios:
                            if (ex.get("grupo") or "").strip().lower() == origem_ref.lower() and (ex.get("subgrupo") or "").strip().lower() == subgrupo_ref.lower():
                                ex["grupo"] = destino_ref

                        salvar_exercicios(exercicios)
                        sucesso = "Subgrupo movido com sucesso."

        elif acao == "aparelho_adicionar":
            if not valor:
                erro = "Informe o nome do aparelho."
            elif any(a.lower() == valor.lower() for a in aparelhos):
                erro = "Este aparelho já existe."
            else:
                aparelhos.append(valor)
                sucesso = "Aparelho adicionado com sucesso."

        elif acao == "aparelho_editar":
            if not valor or not novo_valor:
                erro = "Selecione o aparelho e informe o novo nome."
            elif any(a.lower() == novo_valor.lower() and a.lower() != valor.lower() for a in aparelhos):
                erro = "Já existe outro aparelho com este nome."
            else:
                aparelhos = [novo_valor if a.lower() == valor.lower() else a for a in aparelhos]
                for item in vinculos_aparelho:
                    if (item.get("aparelho") or "").strip().lower() == valor.lower():
                        item["aparelho"] = novo_valor
                for ex in exercicios:
                    if (ex.get("aparelho") or "").strip().lower() == valor.lower():
                        ex["aparelho"] = novo_valor
                salvar_exercicios(exercicios)
                sucesso = "Aparelho atualizado com sucesso."

        elif acao == "aparelho_excluir":
            if not valor:
                erro = "Selecione o aparelho para excluir."
            else:
                aparelhos = [a for a in aparelhos if a.lower() != valor.lower()]
                vinculos_aparelho = [
                    item for item in vinculos_aparelho
                    if (item.get("aparelho") or "").strip().lower() != valor.lower()
                ]
                for ex in exercicios:
                    if (ex.get("aparelho") or "").strip().lower() == valor.lower():
                        ex["aparelho"] = "Peso livre"
                salvar_exercicios(exercicios)
                sucesso = "Aparelho excluído com sucesso."

        elif acao == "aparelho_substituir":
            if not valor or not novo_valor:
                erro = "Selecione o aparelho de origem e o aparelho de destino."
            elif valor.lower() == novo_valor.lower():
                erro = "Escolha aparelhos diferentes para a substituição."
            else:
                origem_ref = next((a for a in aparelhos if a.lower() == valor.lower()), "")
                destino_ref = next((a for a in aparelhos if a.lower() == novo_valor.lower()), "")
                if not origem_ref or not destino_ref:
                    erro = "Aparelho de origem ou destino inválido."
                else:
                    for item in vinculos_aparelho:
                        if (item.get("aparelho") or "").strip().lower() == origem_ref.lower():
                            item["aparelho"] = destino_ref

                    for ex in exercicios:
                        if (ex.get("aparelho") or "").strip().lower() == origem_ref.lower():
                            ex["aparelho"] = destino_ref

                    aparelhos = [a for a in aparelhos if a.lower() != origem_ref.lower()]
                    salvar_exercicios(exercicios)
                    sucesso = "Aparelho substituído e consolidado com sucesso."

        elif acao == "aparelho_vincular":
            if not grupo or not subgrupo or not valor:
                erro = "Selecione grupo, subgrupo e aparelho para vincular."
            else:
                grupo_ref = next((g for g in grupos if g.lower() == grupo.lower()), "")
                if not grupo_ref:
                    erro = "Grupo inválido."
                else:
                    subgrupo_ref = next((s for s in subgrupos_por_grupo.get(grupo_ref, []) if s.lower() == subgrupo.lower()), "")
                    aparelho_ref = next((a for a in aparelhos if a.lower() == valor.lower()), "")
                    if not subgrupo_ref:
                        erro = "Subgrupo inválido para o grupo selecionado."
                    elif not aparelho_ref:
                        erro = "Aparelho inválido."
                    elif any(
                        (item.get("grupo") or "").strip().lower() == grupo_ref.lower()
                        and (item.get("subgrupo") or "").strip().lower() == subgrupo_ref.lower()
                        and (item.get("aparelho") or "").strip().lower() == aparelho_ref.lower()
                        for item in vinculos_aparelho
                    ):
                        erro = "Este aparelho já está vinculado ao grupo/subgrupo selecionado."
                    else:
                        vinculos_aparelho.append(
                            {
                                "grupo": grupo_ref,
                                "subgrupo": subgrupo_ref,
                                "aparelho": aparelho_ref,
                            }
                        )
                        sucesso = "Aparelho vinculado com sucesso."

        elif acao == "aparelho_desvincular":
            if not valor:
                erro = "Selecione o vínculo para remover."
            else:
                grupo_link, subgrupo_link, aparelho_link = (valor.split("|||", 2) + ["", "", ""])[:3]
                antes = len(vinculos_aparelho)
                vinculos_aparelho = [
                    item
                    for item in vinculos_aparelho
                    if not (
                        (item.get("grupo") or "").strip().lower() == grupo_link.lower()
                        and (item.get("subgrupo") or "").strip().lower() == subgrupo_link.lower()
                        and (item.get("aparelho") or "").strip().lower() == aparelho_link.lower()
                    )
                ]
                if len(vinculos_aparelho) < antes:
                    sucesso = "Vínculo removido com sucesso."
                else:
                    erro = "Vínculo não encontrado."

        categorias = normalizar_categorias_para_salvar(
            {
                "grupos": grupos,
                "subgrupos_por_grupo": subgrupos_por_grupo,
                "aparelhos": aparelhos,
                "vinculos_aparelho": vinculos_aparelho,
            }
        )
        salvar_categorias_exercicios(categorias)

    subgrupos_lista = []
    for grupo in categorias["grupos"]:
        for subgrupo in categorias["subgrupos_por_grupo"].get(grupo, []):
            subgrupos_lista.append({"grupo": grupo, "subgrupo": subgrupo})

    return render_template(
        "admin_categorias_exercicios.html",
        categorias=categorias,
        subgrupos_lista=subgrupos_lista,
        aparelhos_por_filtros=obter_aparelhos_por_filtros_com_vinculos(exercicios, categorias.get("vinculos_aparelho") or []),
        erro=erro,
        sucesso=sucesso,
    )


@app.route("/admin/exercicios/novo", methods=["GET", "POST"])
def admin_novo_exercicio():
    bloqueio = exigir_admin_ou_redirect()
    if bloqueio:
        return bloqueio

    pastas_midia = listar_pastas_exercicios()
    exercicios = carregar_exercicios()
    ids_existentes = {ex.get("id") for ex in exercicios if ex.get("id")}
    categorias = carregar_categorias_exercicios(exercicios)
    grupos = categorias["grupos"]
    subgrupos_por_grupo = categorias["subgrupos_por_grupo"]
    subgrupos_por_grupo_lower = {
        (grupo or "").strip().lower(): (lista or [])
        for grupo, lista in subgrupos_por_grupo.items()
    }
    subgrupos = sorted(
        {sg for lista in subgrupos_por_grupo.values() for sg in lista},
        key=lambda x: x.lower(),
    )
    aparelhos_por_filtros = obter_aparelhos_por_filtros_com_vinculos(exercicios, categorias.get("vinculos_aparelho") or [])
    aparelhos = categorias["aparelhos"]
    subgrupos_referencia = [sg for lista in subgrupos_por_grupo.values() for sg in (lista or [])]

    valores = {
        "id": "",
        "nome": "",
        "nome_original": "",
        "grupo": "",
        "subgrupo": "",
        "aparelho": "",
        "pasta_midia": pastas_midia[0] if pastas_midia else "",
        "dicas": "",
        "erros": "",
        "observacoes": "",
        "foco": "",
        "musculos_secundarios": "",
        "instrucoes": "",
    }
    erro = ""
    sucesso = ""

    if request.method == "POST":
        for chave in valores.keys():
            valores[chave] = (request.form.get(chave) or "").strip()

        upload = request.files.get("midia_arquivo")
        nome = valores["nome"]
        exercicio_duplicado = encontrar_exercicio_duplicado(
            exercicios,
            nome=valores["nome"],
            grupo=valores["grupo"],
            subgrupo=valores["subgrupo"],
            aparelho=valores["aparelho"],
        )
        sugestao_id, erro_id = gerar_id_exercicio_por_categorias(
            exercicios,
            grupo=valores["grupo"],
            subgrupo=valores["subgrupo"],
            aparelho=valores["aparelho"],
            subgrupos_referencia=subgrupos_referencia,
            aparelhos_referencia=aparelhos,
        )
        ex_id = sugestao_id
        valores["id"] = ex_id

        if not nome:
            erro = "Informe o nome do exercício."
        elif not valores["nome_original"]:
            erro = "Informe o nome original (inglês) do exercício."
        elif exercicio_duplicado:
            erro = "Já existe um exercício com mesmo nome, grupo, subgrupo e aparelho."
        elif erro_id:
            erro = erro_id
        elif not ex_id:
            erro = "Não foi possível gerar o ID do exercício."
        elif ex_id in ids_existentes:
            erro = "Já existe um exercício com este ID."
        elif not valores["pasta_midia"] or valores["pasta_midia"] not in pastas_midia:
            erro = "Selecione uma pasta válida para salvar a mídia."
        elif not upload or not (upload.filename or "").strip():
            erro = "Envie um arquivo de mídia (.gif ou .mp4)."
        else:
            ext = os.path.splitext(upload.filename)[1].lower()
            if ext not in MIDIAS_PERMITIDAS:
                erro = "Formato inválido. Use apenas arquivos .gif ou .mp4."

        if not erro:
            midia_url = salvar_midia_exercicio(ex_id, upload, ext)
            if not midia_url:
                erro = "Não foi possível salvar a mídia enviada. Tente novamente."

        if not erro:

            novo_exercicio = {
                "id": ex_id,
                "nome": nome,
                "nome_original": valores["nome_original"],
                "grupo": valores["grupo"],
                "subgrupo": valores["subgrupo"],
               "midia": midia_url,
                "dicas": normalizar_linhas_texto(valores["dicas"]),
                "erros": normalizar_linhas_texto(valores["erros"]),
                "observacoes": valores["observacoes"],
                "aparelho": valores["aparelho"],
            }

            if valores["foco"]:
                novo_exercicio["foco"] = valores["foco"]
            if valores["musculos_secundarios"]:
                novo_exercicio["musculos_secundarios"] = normalizar_linhas_texto(valores["musculos_secundarios"])
            if valores["instrucoes"]:
                novo_exercicio["instrucoes"] = normalizar_linhas_texto(valores["instrucoes"])

            exercicios.append(novo_exercicio)
            salvar_exercicios(exercicios)
            sucesso = "Exercício cadastrado com sucesso."

            valores = {
                "id": "",
                "nome": "",
                "nome_original": "",
                "grupo": "",
                "subgrupo": "",
                "aparelho": "",
                "pasta_midia": pastas_midia[0] if pastas_midia else "",
                "dicas": "",
                "erros": "",
                "observacoes": "",
                "foco": "",
                "musculos_secundarios": "",
                "instrucoes": "",
            }

    try:
        return render_template(
            "admin_novo_exercicio.html",
            valores=valores,
            erro=erro,
            sucesso=sucesso,
            grupos=grupos,
            subgrupos=subgrupos,
            subgrupos_por_grupo=subgrupos_por_grupo_lower,
            aparelhos=aparelhos,
            aparelhos_por_filtros=aparelhos_por_filtros,
            pastas_midia=pastas_midia,
        )
    except TemplateNotFound:
        return (
            "Template admin_novo_exercicio.html não encontrado. "
            "Confirme se o arquivo existe na pasta templates no servidor/deploy.",
            500,
        )


@app.route("/admin/exercicios/sugerir-id")
def admin_sugerir_id_exercicio():
    bloqueio = exigir_admin_ou_redirect()
    if bloqueio:
        return jsonify({"erro": "Não autorizado."}), 401

    grupo = (request.args.get("grupo") or "").strip()
    subgrupo = (request.args.get("subgrupo") or "").strip()
    aparelho = (request.args.get("aparelho") or "").strip()
    id_atual = (request.args.get("id_atual") or "").strip()

    if not grupo or not subgrupo or not aparelho:
        return jsonify({"id": "", "erro": "Selecione grupo, subgrupo e aparelho."})

    exercicios = carregar_exercicios()
    categorias = carregar_categorias_exercicios(exercicios)
    subgrupos_ref = [sg for lista in (categorias.get("subgrupos_por_grupo") or {}).values() for sg in (lista or [])]

    sugestao_id, erro = gerar_id_exercicio_por_categorias(
        exercicios,
        grupo=grupo,
        subgrupo=subgrupo,
        aparelho=aparelho,
        id_exercicio_atual=id_atual,
        subgrupos_referencia=subgrupos_ref,
        aparelhos_referencia=categorias.get("aparelhos") or [],
    )

    return jsonify({"id": sugestao_id, "erro": erro})


@app.route("/admin/exercicios/gerar-campos-ia", methods=["POST"])
def admin_gerar_campos_ia_exercicio():
    bloqueio = exigir_admin_ou_redirect()
    if bloqueio:
        return jsonify({"erro": "Não autorizado."}), 401

    payload = request.get_json(silent=True) or {}
    nome = (payload.get("nome") or request.form.get("nome") or "").strip()
    nome_original = (payload.get("nome_original") or request.form.get("nome_original") or "").strip()
    grupo = (payload.get("grupo") or request.form.get("grupo") or "").strip()
    subgrupo = (payload.get("subgrupo") or request.form.get("subgrupo") or "").strip()
    aparelho = (payload.get("aparelho") or request.form.get("aparelho") or "").strip()

    if not nome or not nome_original or not grupo or not subgrupo or not aparelho:
        return jsonify({"duplicado": False, "campos": {}, "mensagem": "Preencha nome em português, nome original, grupo, subgrupo e aparelho."}), 400


    exercicios = carregar_exercicios()
    duplicado = encontrar_exercicio_duplicado(exercicios, nome, grupo, subgrupo, aparelho)
    if duplicado:
        return jsonify(
            {
                "duplicado": True,
                "mensagem": "Já existe um exercício com o mesmo nome, grupo, subgrupo e aparelho.",
                "campos": {},
            }
        )

    campos, erro_ia = gerar_campos_com_ia(nome, nome_original, grupo, subgrupo, aparelho)
    if erro_ia:
        return jsonify({"duplicado": False, "campos": {}, "mensagem": erro_ia}), 400

    return jsonify(
        {
            "duplicado": False,
            "mensagem": "Campos gerados com IA com sucesso.",
            "campos": campos,
        }
    )
@app.route("/midia/exercicios/<caminho>")
def obter_midia_exercicio(caminho):
    if not db or not ExercicioMidiaDB:
        abort(404)

    nome = os.path.basename((caminho or "").strip())
    ex_id, ext = os.path.splitext(nome)
    if not ex_id or ext.lower() not in MIDIAS_PERMITIDAS:
        abort(404)

    registro = ExercicioMidiaDB.query.get(ex_id)
    if not registro:
        abort(404)

    if (registro.extensao or "").lower() != ext.lower():
        abort(404)

    return send_file(
        io.BytesIO(registro.conteudo),
        mimetype=registro.mimetype,
        download_name=f"{ex_id}{ext.lower()}",
        as_attachment=False,
    )


@app.route("/admin/exercicios/<ex_id>/editar", methods=["GET", "POST"])
def admin_editar_exercicio(ex_id):
    bloqueio = exigir_admin_ou_redirect()
    if bloqueio:
        return bloqueio

    exercicios = carregar_exercicios()
    exercicio_atual = next((ex for ex in exercicios if (ex.get("id") or "").strip() == ex_id), None)
    if not exercicio_atual:
        abort(404)

    categorias = carregar_categorias_exercicios(exercicios)
    grupos = categorias["grupos"]
    subgrupos_por_grupo = categorias["subgrupos_por_grupo"]
    subgrupos_por_grupo_lower = {
        (grupo or "").strip().lower(): (lista or [])
        for grupo, lista in subgrupos_por_grupo.items()
    }
    subgrupos = sorted({sg for lista in subgrupos_por_grupo.values() for sg in lista}, key=lambda x: x.lower())
    aparelhos = categorias["aparelhos"]
    aparelhos_por_filtros = obter_aparelhos_por_filtros_com_vinculos(exercicios, categorias.get("vinculos_aparelho") or [])
    subgrupos_referencia = [sg for lista in subgrupos_por_grupo.values() for sg in (lista or [])]
    pastas_midia = listar_pastas_exercicios()

    midia_atual = (exercicio_atual.get("midia") or "").strip()
    pasta_midia_atual = ""
    if midia_atual.startswith("/static/exercicios/"):
        relativo = midia_atual.replace("/static/exercicios/", "", 1)
        partes = relativo.split("/")
        if partes and partes[0]:
            pasta_midia_atual = partes[0]

    if not pasta_midia_atual and pastas_midia:
        pasta_midia_atual = pastas_midia[0]

    valores = {
        "id": exercicio_atual.get("id") or "",
        "nome": exercicio_atual.get("nome") or "",
        "nome_original": exercicio_atual.get("nome_original") or "",
        "grupo": exercicio_atual.get("grupo") or "",
        "subgrupo": exercicio_atual.get("subgrupo") or "",
        "aparelho": exercicio_atual.get("aparelho") or "",
        "pasta_midia": pasta_midia_atual,
        "dicas": "\n".join(exercicio_atual.get("dicas") or []),
        "erros": "\n".join(exercicio_atual.get("erros") or []),
        "observacoes": exercicio_atual.get("observacoes") or "",
        "foco": exercicio_atual.get("foco") or "",
        "musculos_secundarios": "\n".join(exercicio_atual.get("musculos_secundarios") or []),
        "instrucoes": "\n".join(exercicio_atual.get("instrucoes") or []),
    }

    erro = ""
    sucesso = ""

    if request.method == "POST":
        for chave in valores.keys():
            valores[chave] = (request.form.get(chave) or "").strip()

        upload = request.files.get("midia_arquivo")
        id_original = (exercicio_atual.get("id") or "").strip()
        id_novo = id_original
        erro_id = ""

        if (
            valores["grupo"] != (exercicio_atual.get("grupo") or "")
            or valores["subgrupo"] != (exercicio_atual.get("subgrupo") or "")
            or valores["aparelho"] != (exercicio_atual.get("aparelho") or "")
            or not PADRAO_ID_EXERCICIO.fullmatch(id_original)
        ):
            id_novo, erro_id = gerar_id_exercicio_por_categorias(
                exercicios,
                grupo=valores["grupo"],
                subgrupo=valores["subgrupo"],
                aparelho=valores["aparelho"],
                id_exercicio_atual=id_original,
                subgrupos_referencia=subgrupos_referencia,
                aparelhos_referencia=aparelhos,
            )

        valores["id"] = id_novo
        ids_existentes = {
            (ex.get("id") or "").strip()
            for ex in exercicios
            if (ex.get("id") or "").strip() and (ex.get("id") or "").strip() != id_original
        }

        if not valores["nome"]:
            erro = "Informe o nome do exercício."
        elif not valores["nome_original"]:
            erro = "Informe o nome original (inglês) do exercício."
        elif erro_id:
            erro = erro_id
        elif not id_novo:
            erro = "Não foi possível gerar o ID do exercício."
        elif id_novo in ids_existentes:
            erro = "Já existe outro exercício com este ID."
        elif not valores["pasta_midia"] or valores["pasta_midia"] not in pastas_midia:
            erro = "Selecione uma pasta válida para salvar a mídia."

        ext = ""
        if not erro and upload and (upload.filename or "").strip():
            ext = os.path.splitext(upload.filename)[1].lower()
            if ext not in MIDIAS_PERMITIDAS:
                erro = "Formato inválido. Use apenas arquivos .gif ou .mp4."

        if not erro:
            midia_final = midia_atual
            if upload and (upload.filename or "").strip():
                midia_processada = salvar_midia_exercicio(id_novo, upload, ext)
                if not midia_processada:
                    erro = "Não foi possível salvar a mídia enviada. Tente novamente."
                else:
                    midia_final = midia_processada
            elif not midia_final and pastas_midia:
                midia_final = ""

        if not erro:

            exercicio_atual["id"] = id_novo
            exercicio_atual["nome"] = valores["nome"]
            exercicio_atual["nome_original"] = valores["nome_original"]
            exercicio_atual["grupo"] = valores["grupo"]
            exercicio_atual["subgrupo"] = valores["subgrupo"]
            exercicio_atual["aparelho"] = valores["aparelho"]
            exercicio_atual["midia"] = midia_final
            exercicio_atual["dicas"] = normalizar_linhas_texto(valores["dicas"])
            exercicio_atual["erros"] = normalizar_linhas_texto(valores["erros"])
            exercicio_atual["observacoes"] = valores["observacoes"]

            if valores["foco"]:
                exercicio_atual["foco"] = valores["foco"]
            else:
                exercicio_atual.pop("foco", None)

            if valores["musculos_secundarios"]:
                exercicio_atual["musculos_secundarios"] = normalizar_linhas_texto(valores["musculos_secundarios"])
            else:
                exercicio_atual.pop("musculos_secundarios", None)

            if valores["instrucoes"]:
                exercicio_atual["instrucoes"] = normalizar_linhas_texto(valores["instrucoes"])
            else:
                exercicio_atual.pop("instrucoes", None)

            if id_novo != id_original:
                treinos = [normalizar_item_treino(t) for t in carregar_treinos()]
                alterou_treino = False
                for treino in treinos:
                    for item in treino.get("exercicios") or []:
                        if (item.get("exercicio_id") or "").strip() == id_original:
                            item["exercicio_id"] = id_novo
                            alterou_treino = True
                if alterou_treino:
                    salvar_treinos(treinos)

                favoritos = carregar_favoritos_admin()
                if id_original in favoritos:
                    favoritos.remove(id_original)
                    favoritos.add(id_novo)
                    salvar_favoritos_admin(favoritos)

            salvar_exercicios(exercicios)
            sucesso = "Exercício atualizado com sucesso."
            ex_id = id_novo
            midia_atual = midia_final

    return render_template(
        "admin_editar_exercicio.html",
        ex_id=ex_id,
        valores=valores,
        erro=erro,
        sucesso=sucesso,
        grupos=grupos,
        subgrupos=subgrupos,
        subgrupos_por_grupo=subgrupos_por_grupo_lower,
        aparelhos=aparelhos,
        aparelhos_por_filtros=aparelhos_por_filtros,
        pastas_midia=pastas_midia,
        midia_atual=midia_atual,
    )


@app.route("/treino", methods=["GET", "POST"])
def montar_treino():
    bloqueio = exigir_admin_ou_redirect()
    if bloqueio:
        return bloqueio
    exercicios = carregar_exercicios()
    favoritos_admin = carregar_favoritos_admin()
    mapa_exercicios = index_por_id(exercicios)
    grupos = obter_grupos_validos(exercicios)
    subgrupos = obter_subgrupos_validos(exercicios)
    subgrupos_por_grupo = obter_subgrupos_por_grupo(exercicios)
    tipos_treino = TIPOS_TREINO
    objetivos_treino = OBJETIVOS_TREINO
    treinos = [normalizar_item_treino(t) for t in carregar_treinos()]

    busca_treino = (request.args.get("buscar_treino") or "").strip().lower()
    aluno_treino_filtro = (request.args.get("aluno_treino") or "").strip()
    tipo_treino_filtro = (request.args.get("tipo_treino") or "").strip().upper()
    data_inicio_filtro = normalizar_data_iso(request.args.get("data_inicio") or "")
    data_fim_filtro = normalizar_data_iso(request.args.get("data_fim") or "")
    somente_favoritos_treino = (request.args.get("favoritos") or "") == "1"
    mostrar_treinos_salvos = bool(
        busca_treino or aluno_treino_filtro or tipo_treino_filtro or data_inicio_filtro or data_fim_filtro
    )

    alunos_disponiveis = sorted(
        {
            (t.get("aluno") or "").strip()
            for t in treinos
            if (t.get("aluno") or "").strip()
        },
        key=lambda nome: nome.lower(),
    )

    treinos_filtrados = []
    for treino in reversed(treinos):
        treino_tipos = [(t or "").strip().upper() for t in treino.get("tipos", [])]
        treino_tipo = ", ".join(treino_tipos)
        treino_data = normalizar_data_iso(treino.get("data_treino") or treino.get("criado_em"))
        treino_aluno = (treino.get("aluno") or "").strip()
        conteudo_busca = " ".join(
            [
                treino_aluno,
                treino.get("objetivo") or "",
                treino.get("grupo") or "",
                " ".join(treino_tipos),
                treino_data,
            ]
        ).lower()

        if busca_treino and busca_treino not in conteudo_busca:
            continue
        if aluno_treino_filtro and treino_aluno != aluno_treino_filtro:
            continue
        if tipo_treino_filtro and tipo_treino_filtro not in treino_tipos:
            continue
        if data_inicio_filtro and (not treino_data or treino_data < data_inicio_filtro):
            continue
        if data_fim_filtro and (not treino_data or treino_data > data_fim_filtro):
            continue

        preview_exercicios = []
        for idx, item in enumerate(treino.get("exercicios", [])):
            if not isinstance(item, dict):
                continue
            exercicio_id = (item.get("exercicio_id") or "").strip()
            if not exercicio_id:
                continue
            ex = mapa_exercicios.get(exercicio_id)
            if not ex:
                continue

            preview_exercicios.append(
                {
                    "id": exercicio_id,
                    "nome": ex.get("nome") or "Exercício",
                    "grupo": ex.get("grupo") or "",
                    "midia": ex.get("midia") or "",
                    "idx": idx,
                }
            )

        treinos_filtrados.append(
            {
                "id": treino.get("id"),
                "aluno": treino_aluno or "Sem aluno",
                "tipo": treino_tipo or "-",
                "tipos": treino_tipos,
                "objetivo": treino.get("objetivo") or "Não informado",
                "grupo": exibir_lista(treino.get("grupos"), fallback="Não informado"),
                "data_treino": treino_data,
                "total_exercicios": len(treino.get("exercicios") or []),
                "preview_exercicios": preview_exercicios,
                "whatsapp_link": gerar_whatsapp_link_treino(treino.get("id"), treino),
            }
        )

    treino_modelo = None
    treino_edicao = None
    modelo_id = (request.args.get("modelo") or "").strip()
    editar_id = (request.args.get("editar") or "").strip()

    if editar_id:
        treino_edicao = treinos_por_id(treinos).get(editar_id)
        if treino_edicao:
            treino_edicao = normalizar_item_treino(treino_edicao)
    elif modelo_id:
        treino_modelo = treinos_por_id(treinos).get(modelo_id)
        if treino_modelo:
            treino_modelo = normalizar_item_treino(treino_modelo)

    if request.method == "POST":
        aluno = (request.form.get("aluno") or "").strip()
        tipos = normalizar_lista_texto([t.upper() for t in request.form.getlist("tipos[]")])
        objetivo = (request.form.get("objetivo") or "").strip()
        grupos_musculares = normalizar_lista_texto(request.form.getlist("grupos[]"))
        data_treino = normalizar_data_iso(request.form.get("data_treino") or "")

        exercicios_ids = request.form.getlist("exercicio_id[]")
        series_lista = request.form.getlist("series[]")
        repeticoes_lista = request.form.getlist("repeticoes[]")
        minutos_lista = request.form.getlist("minutos[]")
        velocidade_lista = request.form.getlist("velocidade[]")
        tempo_exercicio_lista = request.form.getlist("tempo_exercicio[]")
        descanso_lista = request.form.getlist("descanso[]")

        itens = []
        for idx, ex_id in enumerate(exercicios_ids):
            ex_id = (ex_id or "").strip()
            if not ex_id:
                continue
            series = series_lista[idx] if idx < len(series_lista) else ""
            reps = repeticoes_lista[idx] if idx < len(repeticoes_lista) else ""
            minutos = minutos_lista[idx] if idx < len(minutos_lista) else ""
            velocidade = velocidade_lista[idx] if idx < len(velocidade_lista) else ""
            tempo_exercicio = tempo_exercicio_lista[idx] if idx < len(tempo_exercicio_lista) else ""
            descanso = descanso_lista[idx] if idx < len(descanso_lista) else ""
            itens.append(
                {
                    "exercicio_id": ex_id,
                    "series": (series or "").strip(),
                    "repeticoes": (reps or "").strip(),
                    "minutos": (minutos or "").strip(),
                    "velocidade": (velocidade or "").strip(),
                    "tempo_exercicio": (tempo_exercicio or "").strip(),
                    "descanso": (descanso or "").strip(),
                }
            )

        treinos = [normalizar_item_treino(t) for t in carregar_treinos()]
        treino_id_form = (request.form.get("treino_id") or "").strip()
        treino_id = treino_id_form or uuid.uuid4().hex[:8]
        link_id_form = (request.form.get("link_id") or "").strip()
        link_id = link_id_form or treino_id_form or treino_id

        payload = {
            "id": treino_id,
            "aluno": aluno,
            "tipos": tipos,
            "tipo": exibir_lista(tipos, fallback=""),
            "objetivo": objetivo,
            "grupos": grupos_musculares,
            "grupo": exibir_lista(grupos_musculares, fallback=""),
            "data_treino": data_treino,
            "link_id": link_id,
            "public_token": uuid.uuid4().hex[:12],
            "link_aliases": [],
            "exercicios": itens,
            "criado_em": datetime.utcnow().isoformat(),
        }

        if treino_id_form:
            atualizado = False
            for idx, treino_existente in enumerate(treinos):
                treino_existente = normalizar_item_treino(treino_existente)
                if treino_existente.get("id") == treino_id_form:
                    payload["criado_em"] = treino_existente.get("criado_em") or payload["criado_em"]
                    payload["link_id"] = treino_existente.get("link_id") or payload["link_id"]
                    payload["public_token"] = treino_existente.get("public_token") or payload["public_token"]
                    payload["link_aliases"] = normalizar_lista_texto(
                        (treino_existente.get("link_aliases") or [])
                        + ([treino_existente.get("id")] if treino_existente.get("id") else [])
                    )
                    treinos[idx] = payload
                    atualizado = True
                    break
            if not atualizado:
                treinos.append(payload)
        else:
            treinos.append(payload)

        salvar_treinos(treinos)
        return redirect(url_for("visualizar_treino", treino_id=treino_id))

    exercicios_prefill = [{"exercicio_id": "", "series": "", "repeticoes": "", "minutos": "", "velocidade": "", "tempo_exercicio": "", "descanso": ""}]
    treino_id_prefill = ""
    link_id_prefill = ""
    aluno_prefill = ""
    objetivo_prefill = ""
    grupos_prefill = []
    tipos_prefill = []
    data_treino_prefill = ""

    origem_prefill = treino_edicao or treino_modelo
    if origem_prefill:
        if treino_edicao:
            treino_id_prefill = treino_edicao.get("id") or ""
            aluno_prefill = treino_edicao.get("aluno") or ""
            link_id_prefill = treino_edicao.get("link_id") or treino_id_prefill
        objetivo_prefill = origem_prefill.get("objetivo") or ""
        grupos_prefill = normalizar_lista_texto(origem_prefill.get("grupos") or origem_prefill.get("grupo"))
        tipos_prefill = [t.upper() for t in normalizar_lista_texto(origem_prefill.get("tipos") or origem_prefill.get("tipo"))]
        data_treino_prefill = normalizar_data_iso(origem_prefill.get("data_treino") or origem_prefill.get("criado_em"))
        exercicios_prefill = []
        for item in origem_prefill.get("exercicios") or []:
            if not isinstance(item, dict):
                continue
            exercicios_prefill.append(
                {
                    "exercicio_id": item.get("exercicio_id") or "",
                    "series": item.get("series") or "",
                    "repeticoes": item.get("repeticoes") or "",
                    "minutos": item.get("minutos") or "",
                    "velocidade": item.get("velocidade") or "",
                    "tempo_exercicio": item.get("tempo_exercicio") or "",
                    "descanso": item.get("descanso") or "",
                }
            )

        if not exercicios_prefill:
            exercicios_prefill = [{"exercicio_id": "", "series": "", "repeticoes": "", "minutos": "", "velocidade": "", "tempo_exercicio": "", "descanso": ""}]

    grupos = grupos if isinstance(grupos, list) else []
    opcoes_musculos = sorted(set(grupos) | set(MUSCULOS_ALVO_PADRAO), key=lambda nome: nome.lower())
    aparelhos_por_filtros_treino = obter_aparelhos_por_filtros(exercicios)
    aparelhos_treino = aparelhos_por_filtros_treino["todos"]
    exercicios.sort(
        key=lambda ex: (
            0 if ex.get("id") in favoritos_admin else 1,
            (ex.get("grupo") or ""),
            (ex.get("nome") or ""),
        )
    )

    retorno_url = request.url

    return render_template(
        "treino.html",
        exercicios=exercicios,
        grupos=grupos,
        subgrupos=subgrupos,
        subgrupos_por_grupo=subgrupos_por_grupo,
        aparelhos_por_filtros_treino=aparelhos_por_filtros_treino,
        musculos_opcoes=opcoes_musculos,
        aparelhos_treino=aparelhos_treino,
        tipos_treino=tipos_treino,
        treinos_salvos=treinos_filtrados,
        mostrar_treinos_salvos=mostrar_treinos_salvos,
        alunos_disponiveis=alunos_disponiveis,
        aluno_treino_filtro=aluno_treino_filtro,
        busca_treino=request.args.get("buscar_treino") or "",
        tipo_treino_filtro=tipo_treino_filtro,
        data_inicio_filtro=data_inicio_filtro,
        data_fim_filtro=data_fim_filtro,
        exercicios_prefill=exercicios_prefill,
        treino_id_prefill=treino_id_prefill,
        link_id_prefill=link_id_prefill,
        aluno_prefill=aluno_prefill,
        objetivo_prefill=objetivo_prefill,
        grupos_prefill=grupos_prefill,
        tipos_prefill=tipos_prefill,
        data_treino_prefill=data_treino_prefill,
        objetivos_treino=objetivos_treino,
        treino_modelo=treino_modelo,
        treino_edicao=treino_edicao,
        retorno_url=retorno_url,
        favoritos_admin=favoritos_admin,
        somente_favoritos_treino=somente_favoritos_treino,
    )

@app.route("/treino/<treino_id>")
def visualizar_treino(treino_id):
    exercicios = carregar_exercicios()
    mapa = index_por_id(exercicios)
    treinos = [normalizar_item_treino(t) for t in carregar_treinos()]
    treino = encontrar_treino_por_link(treinos, treino_id)

    if not treino:
        abort(404)

    itens = []
    for item in treino.get("exercicios", []):
        if not isinstance(item, dict):
            continue
        ex = mapa.get(item.get("exercicio_id"))
        if not ex:
            continue
        itens.append(
            {
                "nome": ex.get("nome"),
                "grupo": ex.get("grupo"),
                "series": item.get("series") or "-",
                "repeticoes": item.get("repeticoes") or "-",
                "minutos": item.get("minutos") or "-",
                "velocidade": item.get("velocidade") or "-",
                "tempo_exercicio": item.get("tempo_exercicio") or "-",
                "descanso": item.get("descanso") or "-",
                "aparelho": ex.get("aparelho") or "",
                "midia": ex.get("midia") or "",
                "dicas": ex.get("dicas") or [],
                "erros": ex.get("erros") or [],
                "observacoes": ex.get("observacoes") or "",
            }
        )

    destino = url_for("visualizar_treino", treino_id=treino.get("link_id") or treino_id, _external=True)
    mensagem = (
        f"Treino do aluno {treino.get('aluno') or 'Sem nome'}\n"
        f"Tipo(s): {exibir_lista(treino.get('tipos') or treino.get('tipo'))}\n"
        f"Objetivo: {treino.get('objetivo') or 'Não informado'}\n"
        f"Músculos: {exibir_lista(treino.get('grupos') or treino.get('grupo'), fallback='Não informado')}\n"
        f"Data: {normalizar_data_iso(treino.get('data_treino') or treino.get('criado_em')) or '-'}\n"
        f"Link para visualizar: {destino}"
    )
    whatsapp_link = f"https://wa.me/?text={quote(mensagem)}"

    return render_template(
        "treino_view.html",
        treino=treino,
        itens=itens,
        whatsapp_link=whatsapp_link,
        destino=destino,
    )

@app.route("/treino/<treino_id>/excluir", methods=["POST"])
def excluir_treino(treino_id):
    bloqueio = exigir_admin_ou_redirect()
    if bloqueio:
        return bloqueio
    treinos = [normalizar_item_treino(t) for t in carregar_treinos()]
    treinos_filtrados = [t for t in treinos if t.get("id") != treino_id]

    if len(treinos_filtrados) == len(treinos):
        abort(404)

    salvar_treinos(treinos_filtrados)
    return redirect(url_for("montar_treino"))

@app.route("/e/<ex_id>")
def exercicio(ex_id):
    exercicios = carregar_exercicios()
    mapa = index_por_id(exercicios)

    ex = mapa.get(ex_id)
    if not ex:
        abort(404)

    treino_id = (request.args.get("treino") or "").strip()
    idx_atual = request.args.get("idx")
    voltar_url = (request.args.get("voltar") or "").strip() or url_for("index")

    prev_url = None
    next_url = None
    prescricao_atual = None

    if treino_id:
        treinos = [normalizar_item_treino(t) for t in carregar_treinos()]
        treino = treinos_por_id(treinos).get(treino_id)
        if treino:
            ordem_exercicios = []
            itens_validos = []
            for item in treino.get("exercicios", []):
                if not isinstance(item, dict):
                    continue
                ex_treino_id = (item.get("exercicio_id") or "").strip()
                if ex_treino_id and ex_treino_id in mapa:
                    ordem_exercicios.append(ex_treino_id)
                    itens_validos.append(item)

            if idx_atual is not None:
                try:
                    posicao = int(idx_atual)
                except (TypeError, ValueError):
                    posicao = -1
            else:
                posicao = -1

            if not (0 <= posicao < len(ordem_exercicios)) and ex_id in ordem_exercicios:
                posicao = ordem_exercicios.index(ex_id)

            if 0 <= posicao < len(ordem_exercicios):
                if posicao > 0:
                    prev_id = ordem_exercicios[posicao - 1]
                    prev_url = url_for(
                        "exercicio",
                        ex_id=prev_id,
                        treino=treino_id,
                        idx=posicao - 1,
                        voltar=voltar_url,
                    )
                if posicao < len(ordem_exercicios) - 1:
                    next_id = ordem_exercicios[posicao + 1]
                    next_url = url_for(
                        "exercicio",
                        ex_id=next_id,
                        treino=treino_id,
                        idx=posicao + 1,
                        voltar=voltar_url,
                    )

            item_prescricao = None
            if 0 <= posicao < len(itens_validos):
                item_candidato = itens_validos[posicao]
                if (item_candidato.get("exercicio_id") or "").strip() == ex_id:
                    item_prescricao = item_candidato

            if item_prescricao is None:
                for item in itens_validos:
                    if (item.get("exercicio_id") or "").strip() == ex_id:
                        item_prescricao = item
                        break

            if item_prescricao:
                aparelho = (ex.get("aparelho") or "").strip().lower()
                nome_exercicio = (ex.get("nome") or "").strip().lower()
                cardio_maquina = (
                    aparelho in ["esteira", "bicicleta"]
                    or "esteira" in nome_exercicio
                    or "bicicleta" in nome_exercicio
                    or "bike" in nome_exercicio
                )
                corpo_livre = aparelho == "corpo"

                prescricao_atual = {
                    "tipo": "cardio" if cardio_maquina else ("corpo" if corpo_livre else "forca"),
                    "series": item_prescricao.get("series") or "-",
                    "repeticoes": item_prescricao.get("repeticoes") or "-",
                    "minutos": item_prescricao.get("minutos") or "-",
                    "velocidade": item_prescricao.get("velocidade") or "-",
                    "tempo_exercicio": item_prescricao.get("tempo_exercicio") or "-",
                    "descanso": item_prescricao.get("descanso") or "-",
                }

    return render_template(
        "exercicio.html",
        ex=ex,
        prev_url=prev_url,
        next_url=next_url,
        voltar_url=voltar_url,
        prescricao_atual=prescricao_atual,
    )


@app.route("/qr/<ex_id>.png")
def qr_exercicio(ex_id):
    """
    Gera QR Code PNG que aponta para a página do exercício.
    Dica: em produção, use url_for(..., _external=True) com host correto.
    """
    exercicios = carregar_exercicios()
    mapa = index_por_id(exercicios)

    ex = mapa.get(ex_id)
    if not ex:
        abort(404)

    # URL absoluta (melhor pra QR). Em dev funciona também.
    destino = url_for("exercicio", ex_id=ex_id, _external=True)

    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(destino)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return send_file(buf, mimetype="image/png")


@app.route("/questionario", methods=["GET", "POST"])
def questionario_aluno():
    if request.method == "POST":
        respostas = carregar_respostas_questionario()
        registro = {
            "id": uuid.uuid4().hex[:8],
            "criado_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "nome": limpar_texto_campo(request.form.get("nome")),
            "whatsapp": limpar_texto_campo(request.form.get("whatsapp")),
            "objetivo_principal": limpar_texto_campo(request.form.get("objetivo_principal")),
            "disponibilidade": limpar_texto_campo(request.form.get("disponibilidade")),
            "experiencia_nivel": limpar_texto_campo(request.form.get("experiencia_nivel")),
            "limitacoes": limpar_texto_campo(request.form.get("limitacoes")),
            "enfase_muscular": limpar_texto_campo(request.form.get("enfase_muscular")),
            "rotina": limpar_texto_campo(request.form.get("rotina")),
        }
        respostas.append(registro)
        salvar_respostas_questionario(respostas)
        return redirect(url_for("questionario_aluno", salvo="1"))

    respostas_ordenadas = []
    nome_filtro = ""
    data_inicio_filtro = ""
    data_fim_filtro = ""

    if admin_ativo():
        respostas = carregar_respostas_questionario()
        nome_filtro = limpar_texto_campo(request.args.get("nome") or "")
        data_inicio_filtro = normalizar_data_iso(request.args.get("data_inicio") or "")
        data_fim_filtro = normalizar_data_iso(request.args.get("data_fim") or "")

        respostas_filtradas = []
        for resposta in respostas:
            if not isinstance(resposta, dict):
                continue

            nome_resposta = limpar_texto_campo(resposta.get("nome"))
            data_resposta = normalizar_data_iso(resposta.get("criado_em"))

            if nome_filtro and nome_filtro.lower() not in nome_resposta.lower():
                continue
            if data_inicio_filtro and (not data_resposta or data_resposta < data_inicio_filtro):
                continue
            if data_fim_filtro and (not data_resposta or data_resposta > data_fim_filtro):
                continue

            respostas_filtradas.append(resposta)

        respostas_ordenadas = list(reversed(respostas_filtradas))
    link_formulario = url_for("questionario_aluno", _external=True)
    mensagem = (
        "Oi! Para montar seu treino com mais precisão, responda este formulário rápido:\n"
        f"{link_formulario}"
    )
    whatsapp_link = f"https://wa.me/?text={quote(mensagem)}"

    return render_template(
        "questionario.html",
        salvo=request.args.get("salvo") == "1",
        respostas=respostas_ordenadas,
        link_formulario=link_formulario,
        whatsapp_link=whatsapp_link,
        pode_ver_respostas=admin_ativo(),
        nome_filtro=nome_filtro,
        data_inicio_filtro=data_inicio_filtro,
        data_fim_filtro=data_fim_filtro,
    )

@app.route("/admin/diagnostico-banco")
def diagnostico_banco():
    bloqueio = exigir_admin_ou_redirect()
    if bloqueio:
        return bloqueio

    payload = {
        "db_ativo": bool(db),
        "origem": database_url_origem,
        "url_mascarada": mascara_database_url(database_url),
        "variaveis_detectadas": sorted(set(database_urls_detectadas)),
        "contagens": {"treinos": 0, "respostas": 0},
    }

    if db:
        try:
            payload["contagens"]["treinos"] = TreinoDB.query.count()
            payload["contagens"]["respostas"] = RespostaDB.query.count()
        except SQLAlchemyError as e:
            payload["erro"] = str(e)

    return payload

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    erro = ""
    proximo = (request.args.get("proximo") or request.form.get("proximo") or "").strip()

    if request.method == "POST":
        senha = (request.form.get("senha") or "").strip()
        if senha and senha == ADMIN_PASSWORD:
            session["admin_logado"] = True
            if proximo.startswith("/"):
                return redirect(proximo)
            return redirect(url_for("montar_treino"))
        erro = "Senha inválida."

    return render_template("admin_login.html", erro=erro, proximo=proximo)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_logado", None)
    return redirect(url_for("index"))


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))