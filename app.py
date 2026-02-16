from flask import Flask, render_template, request, abort, send_file, url_for, redirect, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import SQLAlchemyError
import json
import os
import io
import qrcode
from datetime import datetime
import uuid
from urllib.parse import quote

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


def resolver_database_url():
    """Resolve URL do banco em provedores diferentes (Neon/Render/etc)."""
    candidatos = [
        "DATABASE_URL",
        "SQLALCHEMY_DATABASE_URI",
        "NEON_DATABASE_URL",
        "NEONDB_URL",
        "NEONDB",
        "POSTGRES_URL",
        "RENDER_DATABASE_URL",
        "INTERNAL_DATABASE_URL",
        "EXTERNAL_DATABASE_URL",
        "GYM_DATABASE_URL",
    ]

    origem = ""
    valor = ""
    for nome in candidatos:
        candidato = (os.environ.get(nome) or "").strip()
        if candidato:
            origem = nome
            valor = candidato
            break
    if not valor:
        for nome, candidato in os.environ.items():
            chave = (nome or "").strip().upper()
            texto = (candidato or "").strip()
            if not texto:
                continue
            if "DATABASE_URL" in chave or chave.endswith("_DB_URL"):
                origem = nome
                valor = texto
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
            valor = f"postgresql://{pg_user}:{quote(pg_password)}@{pg_host}:{pg_port}/{pg_db}?sslmode=require"

    if valor.startswith("postgres://"):
        valor = f"postgresql://{valor[len('postgres://'):]}"

    return valor, origem


database_url, database_url_origem = resolver_database_url()
if database_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.logger.info(f"Banco configurado via {database_url_origem or 'env'} para persistir treinos/questionários.")
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


    with app.app_context():
        db.create_all()
else:
    TreinoDB = None
    RespostaDB = None


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
    """Carrega a lista de exercícios do JSON."""
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
        ex.setdefault("grupo", "Sem grupo")
        ex.setdefault("midia", "")
        ex.setdefault("dicas", [])
        ex.setdefault("erros", [])
        ex.setdefault("observacoes", "")
        ex.setdefault("aparelho", "Peso livre")
        exercicios_normalizados.append(ex)

    return exercicios_normalizados


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

    q = (request.args.get("q") or "").strip().lower()
    grupo = (request.args.get("grupo") or "").strip().lower()
    aparelho = (request.args.get("aparelho") or "").strip().lower()

    # Lista de grupos/aparelhos (para dropdown)
    # Mantém `grupos` sempre definido para evitar NameError no render
    # mesmo com dados malformados em `exercicios`.
    grupos = obter_grupos_validos(exercicios)
    aparelhos = sorted({(ex.get("aparelho") or "").strip() for ex in exercicios if ex.get("aparelho")})

    filtrados = []
    for ex in exercicios:
        nome = (ex.get("nome") or "").lower()
        g = (ex.get("grupo") or "").lower()

        a = (ex.get("aparelho") or "").lower()

        if grupo and g != grupo:
            continue
        if aparelho and a != aparelho:
            continue
        if q:
            # Busca simples por nome + grupo + aparelho
            if q not in nome and q not in g and q not in a:
                continue

        filtrados.append(ex)

    # Ordena por grupo e nome
    filtrados.sort(key=lambda x: ((x.get("grupo") or ""), (x.get("nome") or "")))

    return render_template(
        "index.html",
        exercicios=filtrados,
        grupos=grupos,
        aparelhos=aparelhos,
        q=request.args.get("q") or "",
        grupo_selecionado=request.args.get("grupo") or "",
        aparelho_selecionado=request.args.get("aparelho") or "",
    )   


@app.route("/treino", methods=["GET", "POST"])
def montar_treino():
    bloqueio = exigir_admin_ou_redirect()
    if bloqueio:
        return bloqueio
    exercicios = carregar_exercicios()
    mapa_exercicios = index_por_id(exercicios)
    grupos = obter_grupos_validos(exercicios)
    tipos_treino = TIPOS_TREINO
    objetivos_treino = OBJETIVOS_TREINO
    treinos = [normalizar_item_treino(t) for t in carregar_treinos()]

    busca_treino = (request.args.get("buscar_treino") or "").strip().lower()
    aluno_treino_filtro = (request.args.get("aluno_treino") or "").strip()
    tipo_treino_filtro = (request.args.get("tipo_treino") or "").strip().upper()
    data_inicio_filtro = normalizar_data_iso(request.args.get("data_inicio") or "")
    data_fim_filtro = normalizar_data_iso(request.args.get("data_fim") or "")
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

        itens = []
        for ex_id, series, reps in zip(exercicios_ids, series_lista, repeticoes_lista):
            ex_id = (ex_id or "").strip()
            if not ex_id:
                continue
            itens.append(
                {
                    "exercicio_id": ex_id,
                    "series": (series or "").strip(),
                    "repeticoes": (reps or "").strip(),
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

    exercicios_prefill = [{"exercicio_id": "", "series": "", "repeticoes": ""}]
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
                }
            )

        if not exercicios_prefill:
            exercicios_prefill = [{"exercicio_id": "", "series": "", "repeticoes": ""}]

    grupos = grupos if isinstance(grupos, list) else []
    opcoes_musculos = sorted(set(grupos) | set(MUSCULOS_ALVO_PADRAO), key=lambda nome: nome.lower())

    retorno_url = request.url

    return render_template(
        "treino.html",
        exercicios=exercicios,
        grupos=opcoes_musculos,
        musculos_opcoes=opcoes_musculos,
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

    if treino_id:
        treinos = [normalizar_item_treino(t) for t in carregar_treinos()]
        treino = treinos_por_id(treinos).get(treino_id)
        if treino:
            ordem_exercicios = []
            for item in treino.get("exercicios", []):
                if not isinstance(item, dict):
                    continue
                ex_treino_id = (item.get("exercicio_id") or "").strip()
                if ex_treino_id and ex_treino_id in mapa:
                    ordem_exercicios.append(ex_treino_id)

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
                    prev_url = url_for("exercicio", ex_id=prev_id, treino=treino_id, idx=posicao - 1, voltar=voltar_url)
                if posicao < len(ordem_exercicios) - 1:
                    next_id = ordem_exercicios[posicao + 1]
                    next_url = url_for("exercicio", ex_id=next_id, treino=treino_id, idx=posicao + 1, voltar=voltar_url)

    return render_template("exercicio.html", ex=ex, prev_url=prev_url, next_url=next_url, voltar_url=voltar_url)


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