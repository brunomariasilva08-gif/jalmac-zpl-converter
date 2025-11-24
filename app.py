import os
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

@app.route('/')
def home():
    return """
    <h1>🚀 Sistema ZPL Jalmac - BACKEND ONLINE!</h1>
    <p>Servidor Flask funcionando perfeitamente!</p>
    <p><a href="/health">✅ Testar Health Check</a></p>
    <p><strong>Próximo passo:</strong> Integrar com frontend v0.dev</p>
    """

@app.route('/health')
def health():
    return jsonify({
        "status": "online",
        "service": "ZPL to PDF Converter - Jalmac Móveis",
        "version": "1.0.0"
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)