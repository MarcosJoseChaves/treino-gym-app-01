from flask import Flask, render_template, request, abort, send_file, url_for
import json
import os
import io
import qrcode

app = Flask(__name__)

DATA_FILE = os.path.join(os.path.dirname(__file__), "exercicios.json")


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
    # Para acessar no celular na mesma rede:
    # app.run(host="0.0.0.0", port=5000, debug=True)
    app.run(debug=True)

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))