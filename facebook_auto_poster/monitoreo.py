from flask import Flask, request, jsonify

app = Flask(__name__)

# IMPORTANTE: Pon aquí la misma clave que configuraste en tu SKILL de OpenClaw
CLAVE_SECRETA = "clave_openclaw_segura_aqui"

def validar_auth():
    """Verifica que OpenClaw envíe el header correcto"""
    clave_recibida = request.headers.get('X-API-Key')
    if clave_recibida != CLAVE_SECRETA:
        print(f"❌ Intento bloqueado. Clave recibida: {clave_recibida}")
        return False
    return True

@app.route('/accounts', methods=['GET'])
def mock_accounts():
    if not validar_auth():
        return jsonify({"error": "Unauthorized"}), 401
    
    print("\n🟢 [GET] /accounts - OpenClaw está consultando las cuentas")
    
    # Devolvemos un JSON simulado basado en tu guía
    return jsonify({
        "accounts": [
            {"name": "maria", "groups": ["1111", "2222"]},
            {"name": "zofia", "groups": ["3333"]}
        ]
    }), 200

@app.route('/post', methods=['POST'])
def mock_post():
    if not validar_auth():
        return jsonify({"error": "Unauthorized"}), 401
    
    payload = request.json
    print("\n🔵 [POST] /post - ¡OpenClaw envió una orden de publicación!")
    print(f"📦 Datos recibidos: {payload}")
    
    # Simulamos que aceptamos el trabajo y devolvemos un ID falso
    return jsonify({
        "status": "accepted",
        "job_id": "job_prueba_12345"
    }), 202

if __name__ == '__main__':
    print("🚀 API Provisional (MOCK) arrancando en el puerto 5000...")
    print("Esperando peticiones de OpenClaw...\n")
    app.run(host='0.0.0.0', port=5000)