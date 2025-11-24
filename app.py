"""
Sistema de Conversão ZPL para PDF - Jalmac Móveis
Versão PRODUÇÃO com WebSocket em Tempo Real
Desenvolvido por: Bruno
"""
import os  # ← Adicionar esta linha
import logging
import re
from datetime import datetime
from pathlib import Path
from time import sleep
from typing import List, Optional
from flask_cors import CORS

import requests
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'jalmac-moveis-secret-key-2025')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

# 🔥 ADICIONE ESTAS LINHAS PARA CORS
CORS(app, origins=[
    "https://*.vercel.app",
    "https://*.v0.dev",
    "http://localhost:3000",  # Para desenvolvimento
    "http://localhost:5173"   # Para Vite/React
])

# ============================================================================
# CONFIGURAÇÃO DO LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURAÇÃO DO FLASK + SOCKETIO
# ============================================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = 'jalmac-moveis-secret-key-2025'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')


# ============================================================================
# CONFIGURAÇÕES DO SISTEMA
# ============================================================================
class Config:
    """Configurações centralizadas do sistema"""

    # API Labelary
    LABELARY_URL = "http://api.labelary.com/v1/printers/8dpmm/labels/4x6/0/"
    REQUEST_TIMEOUT = 30
    RATE_LIMIT_DELAY = 0.4  # Segundos entre requests

    # Limites
    MAX_FILES = 3
    MAX_LABELS_PER_FILE = 50
    ALLOWED_EXTENSIONS = {'zpl', 'txt'}

    # Diretórios
    BASE_DIR = Path(__file__).parent
    UPLOAD_FOLDER = BASE_DIR / 'uploads'
    OUTPUT_FOLDER = BASE_DIR / 'pdfs_temp'
    FINAL_FOLDER = BASE_DIR / 'Etiquetas_Impressao'

    # Criar diretórios se não existirem
    @classmethod
    def init_folders(cls):
        for folder in [cls.UPLOAD_FOLDER, cls.OUTPUT_FOLDER, cls.FINAL_FOLDER]:
            folder.mkdir(exist_ok=True, parents=True)


Config.init_folders()


# ============================================================================
# PROCESSADOR ZPL OTIMIZADO
# ============================================================================
class ZPLProcessor:
    """Processador de arquivos ZPL com validação e normalização"""

    # Padrões regex pré-compilados para performance
    PATTERN_ETIQUETA = re.compile(r'(~DG[^\^]*)?(\^XA.*?\^XZ)', re.DOTALL | re.IGNORECASE)
    PATTERN_DELETE = re.compile(r'\^ID[RG]:', re.IGNORECASE)
    PATTERN_COMANDOS = re.compile(r'\^(?:FO|FT|GF|XG|FB|BC)', re.IGNORECASE)
    PATTERN_CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]')
    PATTERN_SPACES = re.compile(r'\n\s+\^')
    PATTERN_NEWLINES = re.compile(r'\n{3,}')

    @staticmethod
    def normalizar_zpl(zpl: str) -> str:
        """Remove caracteres inválidos e normaliza formatação"""
        zpl = ZPLProcessor.PATTERN_CONTROL_CHARS.sub('', zpl)
        zpl = ZPLProcessor.PATTERN_SPACES.sub('\n^', zpl)
        zpl = ZPLProcessor.PATTERN_NEWLINES.sub('\n\n', zpl)
        return zpl.strip()

    @staticmethod
    def validar_etiqueta(zpl: str) -> bool:
        """Valida se o ZPL contém comandos essenciais"""
        zpl_upper = zpl.upper()
        return (
                len(zpl) > 20 and
                '^XA' in zpl_upper and
                '^XZ' in zpl_upper and
                bool(ZPLProcessor.PATTERN_COMANDOS.search(zpl))
        )

    @staticmethod
    def preparar_etiqueta(zpl: str) -> str:
        """Adiciona comandos obrigatórios se não existirem"""
        zpl = ZPLProcessor.normalizar_zpl(zpl)
        zpl_upper = zpl.upper()

        # Adiciona início e fim se não tiver
        if not zpl_upper.startswith("^XA"):
            zpl = f"^XA\n{zpl}"
        if not zpl_upper.endswith("^XZ"):
            zpl = f"{zpl}\n^XZ"

        # Adiciona configurações padrão
        if "^PW" not in zpl_upper:
            zpl = zpl.replace("^XA", "^XA\n^PW812", 1)
        if "^LL" not in zpl_upper:
            zpl = zpl.replace("^XA", "^XA\n^LL1218", 1)
        if "^PQ" not in zpl_upper:
            zpl = zpl.replace("^XZ", "^PQ1\n^XZ")

        return zpl

    @staticmethod
    def extrair_etiquetas(texto_zpl: str) -> List[str]:
        """Extrai todas as etiquetas válidas do texto ZPL"""
        texto_zpl = ZPLProcessor.normalizar_zpl(texto_zpl)
        matches = ZPLProcessor.PATTERN_ETIQUETA.finditer(texto_zpl)

        etiquetas_validas = []

        for i, match in enumerate(matches, 1):
            recurso = match.group(1) or ""
            bloco_zpl = match.group(2)
            etiqueta_completa = recurso + bloco_zpl

            # Ignora comandos de delete
            if ZPLProcessor._is_comando_delete(bloco_zpl):
                continue

            # Valida etiqueta
            if not ZPLProcessor.validar_etiqueta(bloco_zpl):
                logger.warning(f"Etiqueta {i} inválida - ignorada")
                continue

            # Prepara e adiciona
            etiqueta_preparada = ZPLProcessor.preparar_etiqueta(etiqueta_completa)
            etiquetas_validas.append(etiqueta_preparada)

        logger.info(f"Extraídas {len(etiquetas_validas)} etiquetas válidas")
        return etiquetas_validas

    @staticmethod
    def _is_comando_delete(zpl: str) -> bool:
        """Verifica se é um comando de delete de recurso"""
        return bool(ZPLProcessor.PATTERN_DELETE.search(zpl)) and len(zpl) < 50


# ============================================================================
# CONVERSOR PARA PDF
# ============================================================================
class PDFConverter:
    """Conversor de ZPL para PDF usando API Labelary"""

    def __init__(self):
        self.pdf_files: List[Path] = []
        self.session = requests.Session()  # Reutiliza conexões

    def converter_etiqueta(self, zpl: str, index: int, total: int) -> Optional[Path]:
        """
        Converte uma etiqueta ZPL para PDF via API Labelary
        Emite progresso via WebSocket
        """
        try:
            # Delay para respeitar rate limit da API
            sleep(Config.RATE_LIMIT_DELAY)

            # Emite progresso via WebSocket
            progress_percent = int((index / total) * 100)
            socketio.emit('progress_update', {
                'processed': index,
                'total': total,
                'progress': progress_percent,
                'status': f'Processando etiqueta {index}/{total}...'
            })

            # Faz request para API Labelary
            response = self.session.post(
                Config.LABELARY_URL,
                data=zpl.encode("utf-8"),
                headers={"Accept": "application/pdf"},
                timeout=Config.REQUEST_TIMEOUT
            )

            if response.status_code == 200:
                # Salva PDF temporário
                pdf_path = Config.OUTPUT_FOLDER / f"etiqueta_{index:04d}.pdf"
                pdf_path.write_bytes(response.content)

                size_kb = len(response.content) / 1024
                logger.info(f"✓ Etiqueta {index}/{total} convertida ({size_kb:.1f} KB)")
                return pdf_path
            else:
                logger.error(f"✗ API retornou erro {response.status_code} para etiqueta {index}")
                return None

        except Exception as e:
            logger.error(f"✗ Erro ao converter etiqueta {index}: {e}")
            return None

    def converter_lote(self, etiquetas: List[str]) -> List[Path]:
        """
        Converte uma lista de etiquetas em sequência
        Retorna lista de PDFs gerados com sucesso
        """
        total = len(etiquetas)
        logger.info(f"Iniciando conversão de {total} etiquetas")

        # Emite início do processamento
        socketio.emit('progress_update', {
            'processed': 0,
            'total': total,
            'progress': 0,
            'status': 'Iniciando conversão...'
        })

        # Converte etiquetas uma por uma
        for i, zpl in enumerate(etiquetas, 1):
            pdf_path = self.converter_etiqueta(zpl, i, total)
            if pdf_path:
                self.pdf_files.append(pdf_path)

        logger.info(f"Conversão finalizada: {len(self.pdf_files)}/{total} etiquetas")
        return self.pdf_files

    def mesclar_pdfs(self, nome_arquivo: str = None) -> Optional[Path]:
        """
        Mescla todos os PDFs em um único arquivo
        Retorna caminho do PDF final
        """
        if not self.pdf_files:
            logger.error("Nenhum PDF para mesclar")
            return None

        try:
            from PyPDF2 import PdfMerger

            # Emite status de mesclagem
            socketio.emit('progress_update', {
                'processed': len(self.pdf_files),
                'total': len(self.pdf_files),
                'progress': 95,
                'status': f'Mesclando {len(self.pdf_files)} PDFs...'
            })

            logger.info(f"Mesclando {len(self.pdf_files)} PDFs...")

            merger = PdfMerger()

            # Adiciona PDFs em ordem
            for pdf_path in sorted(self.pdf_files):
                merger.append(str(pdf_path))

            # Define nome do arquivo final
            if nome_arquivo is None:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                nome_arquivo = f"etiquetas_{timestamp}.pdf"

            # Salva PDF final
            caminho_final = Config.FINAL_FOLDER / nome_arquivo
            merger.write(str(caminho_final))
            merger.close()

            # Limpa arquivos temporários
            self._limpar_temporarios()

            size_kb = caminho_final.stat().st_size / 1024
            logger.info(f"✓ PDF final gerado: {caminho_final.name} ({size_kb:.1f} KB)")

            return caminho_final

        except Exception as e:
            logger.error(f"✗ Erro ao mesclar PDFs: {e}")
            return None

    def _limpar_temporarios(self):
        """Remove PDFs temporários após mesclagem"""
        for pdf in self.pdf_files:
            try:
                pdf.unlink()
            except:
                pass

        # Remove pasta temporária se vazia
        try:
            if Config.OUTPUT_FOLDER.exists() and not any(Config.OUTPUT_FOLDER.iterdir()):
                Config.OUTPUT_FOLDER.rmdir()
        except:
            pass


# ============================================================================
# ROTAS WEB
# ============================================================================

@app.route('/')
def index():
    """Página principal do sistema"""
    return render_template('index.html')


@app.route('/health')
def health():
    """Endpoint de verificação de saúde do sistema"""
    return jsonify({
        'status': 'online',
        'service': 'ZPL to PDF Converter - Jalmac Móveis',
        'version': '1.0.0',
        'timestamp': datetime.now().isoformat()
    })


# ============================================================================
# EVENTOS WEBSOCKET
# ============================================================================

@socketio.on('connect')
def handle_connect():
    """Cliente conectado via WebSocket"""
    logger.info(f"Cliente conectado: {request.sid}")
    emit('connected', {'message': 'Conectado ao servidor'})


@socketio.on('disconnect')
def handle_disconnect():
    """Cliente desconectado"""
    logger.info(f"Cliente desconectado: {request.sid}")


@socketio.on('start_conversion')
def handle_conversion(data):
    """
    Evento principal: inicia conversão de arquivos ZPL
    Recebe base64 dos arquivos via WebSocket
    """
    try:
        logger.info("=== Iniciando nova conversão ===")

        files_data = data.get('files', [])

        if not files_data:
            emit('error', {'message': 'Nenhum arquivo enviado'})
            return

        if len(files_data) > Config.MAX_FILES:
            emit('error', {'message': f'Máximo de {Config.MAX_FILES} arquivos por vez'})
            return

        # Processa todos os arquivos
        todas_etiquetas = []

        for i, file_data in enumerate(files_data, 1):
            filename = file_data.get('name', f'arquivo_{i}')
            content_b64 = file_data.get('content', '')

            # Decodifica conteúdo base64
            import base64
            try:
                content = base64.b64decode(content_b64).decode('utf-8', errors='ignore')
            except:
                logger.warning(f"Erro ao decodificar arquivo {filename}")
                continue

            # Extrai etiquetas
            etiquetas = ZPLProcessor.extrair_etiquetas(content)

            if etiquetas:
                todas_etiquetas.extend(etiquetas)
                logger.info(f"Arquivo {filename}: {len(etiquetas)} etiquetas")

        if not todas_etiquetas:
            emit('error', {'message': 'Nenhuma etiqueta válida encontrada nos arquivos'})
            return

        # Limita total de etiquetas
        if len(todas_etiquetas) > Config.MAX_LABELS_PER_FILE * Config.MAX_FILES:
            todas_etiquetas = todas_etiquetas[:Config.MAX_LABELS_PER_FILE * Config.MAX_FILES]

        logger.info(f"Total de etiquetas para processar: {len(todas_etiquetas)}")

        # Converte etiquetas para PDF
        converter = PDFConverter()
        converter.converter_lote(todas_etiquetas)

        if not converter.pdf_files:
            emit('error', {'message': 'Nenhum PDF foi gerado com sucesso'})
            return

        # Mescla PDFs
        pdf_final = converter.mesclar_pdfs()

        if not pdf_final:
            emit('error', {'message': 'Erro ao mesclar PDFs'})
            return

        # Emite conclusão
        socketio.emit('conversion_complete', {
            'success': True,
            'filename': pdf_final.name,
            'total_labels': len(todas_etiquetas),
            'file_size': pdf_final.stat().st_size,
            'download_url': f'/download/{pdf_final.name}'
        })

        logger.info("=== Conversão concluída com sucesso ===")

    except Exception as e:
        logger.error(f"Erro crítico na conversão: {e}", exc_info=True)
        emit('error', {'message': f'Erro no servidor: {str(e)}'})


# ============================================================================
# ROTA DE DOWNLOAD
# ============================================================================

@app.route('/download/<filename>')
def download_file(filename):
    """Endpoint para download do PDF gerado"""
    try:
        # Sanitiza nome do arquivo
        safe_filename = secure_filename(filename)
        file_path = Config.FINAL_FOLDER / safe_filename

        if not file_path.exists():
            return jsonify({'error': 'Arquivo não encontrado'}), 404

        return send_file(
            file_path,
            as_attachment=True,
            download_name=safe_filename,
            mimetype='application/pdf'
        )

    except Exception as e:
        logger.error(f"Erro no download: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# INICIALIZAÇÃO DO SERVIDOR
# ============================================================================

if __name__ == '__main__':
    print("\n" + "=" * 70)
    print("🏷️  SISTEMA DE CONVERSÃO ZPL PARA PDF - JALMAC MÓVEIS")
    print("=" * 70)
    print(f"🚀 Servidor iniciando em http://0.0.0.0:5000")
    print(f"📁 Pasta de saída: {Config.FINAL_FOLDER.absolute()}")
    print(f"🔌 WebSocket ativado para atualizações em tempo real")
    print("=" * 70 + "\n")

    # Inicia servidor com SocketIO e Eventlet
    socketio.run(
        app,
        host='0.0.0.0',
        port=5000,
        debug=False,  # Usar True apenas em desenvolvimento
        use_reloader=False,
        log_output=True
    )