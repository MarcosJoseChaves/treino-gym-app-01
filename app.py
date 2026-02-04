from flask import Flask, render_template, request, abort, send_file, url_for, redirect
import json
import os
import io
import qrcode
from datetime import datetime
import uuid
from urllib.parse import quote

app = Flask(__name__)

DATA_FILE = os.path.join(os.path.dirname(__file__), "exercicios.json")
TREINOS_FILE = os.path.join(os.path.dirname(__file__), "treinos.json")


def carregar_exercicios():
    """Carrega a lista de exercícios do JSON."""
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Garantias mínimas de consistência
    for ex in data:
        ex.setdefault("id", "")
        ex.setdefault("nome", "Sem nome")
        ex.setdefault("grupo", "Sem grupo")
        ex.setdefault("gif", "")
        ex.setdefault("dicas", [])
        ex.setdefault("erros", [])
        ex.setdefault("observacoes", "")
    return data


def index_por_id(exercicios):
    """Mapa id -> exercício."""
    return {ex["id"]: ex for ex in exercicios if ex.get("id")}


def carregar_treinos():
    if not os.path.exists(TREINOS_FILE):
        return []
    with open(TREINOS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def salvar_treinos(treinos):
    with open(TREINOS_FILE, "w", encoding="utf-8") as f:
        json.dump(treinos, f, ensure_ascii=False, indent=2)


def normalizar_item_treino(item):
    item.setdefault("id", "")
    item.setdefault("aluno", "")
    item.setdefault("objetivo", "")
    item.setdefault("grupo", "")
    item.setdefault("exercicios", [])
    item.setdefault("criado_em", "")
    return item


def treinos_por_id(treinos):
    return {t["id"]: t for t in treinos if t.get("id")}


@app.route("/")
def index():
    exercicios = carregar_exercicios()

    q = (request.args.get("q") or "").strip().lower()
    grupo = (request.args.get("grupo") or "").strip().lower()

    # Lista de grupos (para dropdown)
    grupos = sorted({(ex.get("grupo") or "").strip() for ex in exercicios if ex.get("grupo")})

    filtrados = []
    for ex in exercicios:
        nome = (ex.get("nome") or "").lower()
        g = (ex.get("grupo") or "").lower()

        if grupo and g != grupo:
            continue
        if q:
            # Busca simples por nome + grupo
            if q not in nome and q not in g:
                continue

        filtrados.append(ex)

    # Ordena por grupo e nome
    filtrados.sort(key=lambda x: ((x.get("grupo") or ""), (x.get("nome") or "")))

    return render_template(
        "index.html",
        exercicios=filtrados,
        grupos=grupos,
        q=request.args.get("q") or "",
        grupo_selecionado=request.args.get("grupo") or ""
    )


@app.route("/treino", methods=["GET", "POST"])
def montar_treino():
    exercicios = carregar_exercicios()
    grupos = sorted({(ex.get("grupo") or "").strip() for ex in exercicios if ex.get("grupo")})

    if request.method == "POST":
        aluno = (request.form.get("aluno") or "").strip()
        objetivo = (request.form.get("objetivo") or "").strip()
        grupo = (request.form.get("grupo") or "").strip()

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
        treino_id = uuid.uuid4().hex[:8]
        treinos.append(
            {
                "id": treino_id,
                "aluno": aluno,
                "objetivo": objetivo,
                "grupo": grupo,
                "exercicios": itens,
                "criado_em": datetime.utcnow().isoformat(),
            }
        )
        salvar_treinos(treinos)

        return redirect(url_for("visualizar_treino", treino_id=treino_id))

    return render_template(
        "treino.html",
        exercicios=exercicios,
        grupos=grupos,
    )


@app.route("/treino/<treino_id>")
def visualizar_treino(treino_id):
    exercicios = carregar_exercicios()
    mapa = index_por_id(exercicios)
    treinos = [normalizar_item_treino(t) for t in carregar_treinos()]
    treino = treinos_por_id(treinos).get(treino_id)

    if not treino:
        abort(404)

    itens = []
    for item in treino.get("exercicios", []):
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

    destino = url_for("visualizar_treino", treino_id=treino_id, _external=True)
    mensagem = (
        f"Treino do aluno {treino.get('aluno') or 'Sem nome'}\n"
        f"Objetivo: {treino.get('objetivo') or 'Não informado'}\n"
        f"Grupo: {treino.get('grupo') or 'Não informado'}\n"
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


@app.route("/e/<ex_id>")
def exercicio(ex_id):
    exercicios = carregar_exercicios()
    mapa = index_por_id(exercicios)

    ex = mapa.get(ex_id)
    if not ex:
        abort(404)

    return render_template("exercicio.html", ex=ex)


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


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))