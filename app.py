import os
import json
import io
import uuid
import hashlib
import time
from datetime import datetime, timedelta
import threading
import mysql.connector
import requests
from flask import Flask, request, send_file, jsonify, send_from_directory, session, redirect, url_for
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet
import base64
import libtorrent as lt
from openai import OpenAI

app = Flask(__name__)
app.secret_key = 'brothernoahbrothernoah' # Troque isso em produção

# --- CONFIGURAÇÃO DA OPENAI (CHATGPT) ---
OPENAI_API_KEY = "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" # <--- COLE SUA API KEY DA OPENAI AQUI
client = OpenAI(api_key=OPENAI_API_KEY)

# --- SELEÇÃO AUTOMÁTICA DE MODELO ---
ACTIVE_OPENAI_MODEL = 'gpt-4o-mini' # Modelo rápido e barato (recomendado)

def select_best_openai_model():
    """Testa qual modelo da OpenAI está respondendo corretamente na inicialização."""
    global ACTIVE_OPENAI_MODEL
    candidates = [
        'gpt-4o-mini',
        'gpt-3.5-turbo',
        'gpt-4o'
    ]
    
    print("\n--- DIAGNÓSTICO DE IA (INICIALIZAÇÃO) ---")
    
    # Tenta carregar do banco primeiro
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT value FROM system_config WHERE key_name = 'active_openai_model'")
            row = cursor.fetchone()
            if row:
                saved_model = row['value']
                print(f"Verificando modelo salvo no banco ({saved_model})...", end=" ")
                try:
                    client.chat.completions.create(
                        model=saved_model,
                        messages=[{"role": "user", "content": "Oi"}],
                        max_tokens=1
                    )
                    ACTIVE_OPENAI_MODEL = saved_model
                    print("OK! Confirmado.")
                    print("-----------------------------------------\n")
                    conn.close()
                    return
                except Exception as e:
                    print(f"Falha ({saved_model}): {e}. Buscando novo modelo...")
        except Exception as e:
            print(f"Erro ao ler config do banco: {e}")

    for model in candidates:
        try:
            print(f"Testando {model}...", end=" ")
            # Teste rápido com 1 token para validar conexão
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Oi"}],
                max_tokens=1
            )
            ACTIVE_OPENAI_MODEL = model
            print(f"OK! Definido como modelo ativo.")
            
            # Salva no banco para persistência
            if conn and conn.is_connected():
                try:
                    cursor = conn.cursor()
                    cursor.execute("INSERT INTO system_config (key_name, value) VALUES ('active_openai_model', %s) ON DUPLICATE KEY UPDATE value = %s", (model, model))
                    conn.commit()
                    print("Modelo salvo no banco de dados.")
                except Exception as db_err:
                    print(f"Erro ao salvar config: {db_err}")

            print("-----------------------------------------\n")
            if conn and conn.is_connected():
                conn.close()
            return
        except Exception as e:
            print(f"Falha: {e}")
    
    print(f"AVISO: Nenhum modelo respondeu no teste. Mantendo fallback: {ACTIVE_OPENAI_MODEL}")
    print("-----------------------------------------\n")
    if conn and conn.is_connected():
        conn.close()

# --- CONFIGURAÇÃO DO VIRUSTOTAL ---
VIRUSTOTAL_API_KEY = "86b74693f146826ff04c55c36e8afb106ae58108a26c41fb323f7b884b41d1fc" # Obtenha gratuitamente em virustotal.com

# Configurações
UPLOAD_FOLDER = 'storage'
DOLLY_FOLDER = 'dolly_files'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DOLLY_FOLDER, exist_ok=True)

# Chave de criptografia para os arquivos .dolly (Deve ser fixa para poder ler arquivos antigos)
# Em produção, use variáveis de ambiente.
ENCRYPTION_KEY = b'gQjW8_5V4q3z2s1X0o9p8u7y6t5r4e3w2q1a0s9d8f7=' 
cipher_suite = Fernet(ENCRYPTION_KEY)

# --- CONFIGURAÇÃO DO TIDB ---
# Preencha com os dados do seu painel TiDB Cloud
DB_CONFIG = {
    'host': 'gateway01.us-west-2.prod.aws.tidbcloud.com', # Exemplo: troque pelo seu host
    'port': 4000,
    'user': '3jZGJoZm7yRDfbG.root', # Troque pelo seu usuário
    'password': 'zRbX8aXBISsk5Pft', # Troque pela sua senha
    'database': 'test'
}

def get_db_connection():
    """Conecta ao banco de dados TiDB."""
    try:
        return mysql.connector.connect(**DB_CONFIG)
    except mysql.connector.Error as err:
        print(f"Erro de conexão com TiDB: {err}")
        return None

def init_db():
    """Cria a tabela de metadados no TiDB se não existir."""
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS arquivos_dolly (
                hash VARCHAR(64),
                filename VARCHAR(255),
                size_bytes BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                owner_id INT,
                PRIMARY KEY (hash)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                is_admin BOOLEAN DEFAULT FALSE,
                is_approved BOOLEAN DEFAULT FALSE,
                quota_used BIGINT DEFAULT 0,
                is_vip BOOLEAN DEFAULT FALSE,
                vip_expiration DATETIME
            )
        """)

        # Migração de Emergência: Adiciona a coluna owner_id se ela estiver faltando
        try:
            cursor.execute("ALTER TABLE arquivos_dolly ADD COLUMN owner_id INT")
        except mysql.connector.Error as err:
            # Ignora o erro 1060 (Duplicate column name) se a coluna já existir
            if err.errno != 1060:
                print(f"Aviso do Banco de Dados: {err}")

        # Migração 2: Adiciona suporte a Magnet Links (para torrents reais no futuro)
        try:
            cursor.execute("ALTER TABLE arquivos_dolly ADD COLUMN magnet_link TEXT")
        except mysql.connector.Error as err:
            # Ignora erro se a coluna já existir
            if err.errno != 1060:
                print(f"Aviso do Banco de Dados (Magnet): {err}")

        # Migração 3: Adiciona coluna para o CONTEÚDO do arquivo (BLOB)
        try:
            # LONGBLOB suporta até 4GB (teoricamente), mas depende do limite de pacote do servidor
            cursor.execute("ALTER TABLE arquivos_dolly ADD COLUMN file_content LONGBLOB")
        except mysql.connector.Error as err:
            if err.errno != 1060:
                print(f"Aviso do Banco de Dados (Blob): {err}")

        # Migração 4: Tabela para pedaços de arquivos (Chunking) para contornar limite de 6MB do TiDB
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS file_chunks (
                id INT AUTO_INCREMENT PRIMARY KEY,
                file_hash VARCHAR(64),
                chunk_index INT,
                chunk_data LONGBLOB,
                INDEX (file_hash)
            )
        """)

        # Migração 5: Tabela de Mensagens de Suporte (Chat com IA e Admin)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id INT AUTO_INCREMENT PRIMARY KEY,
                session_id VARCHAR(64),
                user_id INT,
                sender VARCHAR(10), -- 'user' ou 'bot'
                message TEXT,
                is_escalated BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migração 6: Adiciona colunas VIP se faltarem
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN is_vip BOOLEAN DEFAULT FALSE")
            cursor.execute("ALTER TABLE users ADD COLUMN vip_expiration DATETIME")
        except mysql.connector.Error as err:
            if err.errno != 1060:
                print(f"Aviso do Banco de Dados (VIP): {err}")

        # Migração 7: Status do VirusTotal
        try:
            cursor.execute("ALTER TABLE arquivos_dolly ADD COLUMN vt_status VARCHAR(50) DEFAULT 'PENDING'")
        except mysql.connector.Error as err:
            if err.errno != 1060:
                print(f"Aviso do Banco de Dados (VT): {err}")

        # Migração 8: Sistema de Banimento
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN is_banned BOOLEAN DEFAULT FALSE")
        except mysql.connector.Error as err:
            if err.errno != 1060:
                print(f"Aviso do Banco de Dados (Ban): {err}")

        # Migração 9: Configurações do Sistema (Persistência de IA)
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_config (
                    key_name VARCHAR(50) PRIMARY KEY,
                    value VARCHAR(255)
                )
            """)
        except mysql.connector.Error as err:
            if err.errno != 1060:
                print(f"Aviso do Banco de Dados (Config): {err}")

        conn.commit()
        cursor.close()
        conn.close()
        print("Banco de dados TiDB conectado e inicializado!")

def calculate_sha256(file_path):
    """Gera um hash único para o arquivo para garantir integridade."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

@app.route('/')
def index():
    return send_file('index.html')

# --- SISTEMA DE LOGIN E ADMIN ---

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password') # Em produção, use hash (bcrypt/argon2)
    
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        try:
            # Se for o usuário "admin", já cria como admin e aprovado
            is_admin = True if username.lower() == 'admin' else False
            is_approved = True if is_admin else False
            
            cursor.execute("INSERT INTO users (username, password, is_admin, is_approved) VALUES (%s, %s, %s, %s)", 
                           (username, password, is_admin, is_approved))
            conn.commit()
            return jsonify({"message": "Registrado com sucesso! Faça login."})
        except mysql.connector.Error as err:
            return jsonify({"error": "Usuário já existe ou erro no banco."}), 400
        finally:
            conn.close()
    return jsonify({"error": "Erro de conexão"}), 500

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE username = %s AND password = %s", (username, password))
        user = cursor.fetchone()
        conn.close()
        
        if user:
            # Verifica se está banido
            if user.get('is_banned'):
                return jsonify({"error": "Conta banida", "banned": True}), 403

            # Verifica expiração do VIP
            if user['is_vip'] and user['vip_expiration']:
                if user['vip_expiration'] < datetime.now():
                    # VIP Expirou
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("UPDATE users SET is_vip = FALSE WHERE id = %s", (user['id'],))
                    conn.commit()
                    conn.close()
                    user['is_vip'] = False

            session.permanent = True # Mantém o login ativo mesmo ao fechar o navegador
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['is_admin'] = user['is_admin']
            session['is_approved'] = user['is_approved']
            session['is_vip'] = user['is_vip']
            session['quota_used'] = user['quota_used']
            return jsonify({"message": "Login realizado", "user": user})
        
    return jsonify({"error": "Credenciais inválidas"}), 401

@app.route('/logout')
def logout():
    session.clear()
    return jsonify({"message": "Logout realizado"})

@app.route('/check_session', methods=['GET'])
def check_session():
    """Verifica se o usuário já está logado."""
    if 'user_id' in session:
        # Verifica no banco se o status de banimento mudou
        conn = get_db_connection()
        if conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT is_banned FROM users WHERE id = %s", (session['user_id'],))
            user_status = cursor.fetchone()
            conn.close()
            if user_status and user_status['is_banned']:
                return jsonify({"logged_in": False, "banned": True})

        return jsonify({
            "logged_in": True,
            "user": {
                "username": session.get('username'),
                "is_admin": session.get('is_admin'),
                "is_approved": session.get('is_approved'),
                "is_vip": session.get('is_vip')
            }
        })
    return jsonify({"logged_in": False})

@app.route('/banned')
def banned_page():
    """Retorna a animação detalhada de banimento."""
    return """
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ACESSO NEGADO</title>
        <style>
            body {
                margin: 0;
                padding: 0;
                background-color: #050505;
                color: #ff0000;
                font-family: 'Courier New', Courier, monospace;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                overflow: hidden;
            }
            .container {
                text-align: center;
                position: relative;
                z-index: 10;
                border: 2px solid #ff0000;
                padding: 40px;
                box-shadow: 0 0 20px #ff0000, inset 0 0 20px #ff0000;
                background: rgba(0, 0, 0, 0.9);
            }
            h1 {
                font-size: 3rem;
                text-transform: uppercase;
                letter-spacing: 5px;
                animation: glitch 0.8s linear infinite;
                margin: 0 0 20px 0;
            }
            p { font-size: 1.2rem; color: #fff; margin-bottom: 30px; }
            .icon { font-size: 5rem; margin-bottom: 20px; display: block; }
            .progress-container {
                width: 100%;
                height: 4px;
                background: #333;
                margin-top: 20px;
            }
            .progress-bar {
                height: 100%;
                background: #ff0000;
                width: 0%;
                animation: load 5s linear forwards;
                box-shadow: 0 0 10px #ff0000;
            }
            @keyframes glitch {
                2%, 64% { transform: translate(2px,0) skew(0deg); }
                4%, 60% { transform: translate(-2px,0) skew(0deg); }
                62% { transform: translate(0,0) skew(5deg); }
            }
            @keyframes load {
                0% { width: 0%; }
                100% { width: 100%; }
            }
            .scanlines {
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                background: linear-gradient(to bottom, rgba(255,255,255,0), rgba(255,255,255,0) 50%, rgba(0,0,0,0.2) 50%, rgba(0,0,0,0.2));
                background-size: 100% 4px;
                pointer-events: none;
                z-index: 5;
            }
        </style>
    </head>
    <body>
        <div class="scanlines"></div>
        <div class="container">
            <span class="icon">🚫</span>
            <h1>USUÁRIO BANIDO</h1>
            <p>VIOLAÇÃO DOS TERMOS DE SERVIÇO DETECTADA.</p>
            <p>Encerrando conexão com o servidor Dolly...</p>
            <div class="progress-container"><div class="progress-bar"></div></div>
        </div>
        <script>
            setTimeout(() => { window.location.href = '/logout'; }, 5000);
        </script>
    </body>
    </html>
    """

@app.route('/admin/pending_users', methods=['GET'])
def list_pending():
    if not session.get('is_admin'):
        return jsonify({"error": "Acesso negado"}), 403
        
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, quota_used FROM users WHERE is_approved = FALSE")
    users = cursor.fetchall()
    conn.close()
    return jsonify(users)

@app.route('/admin/approve/<int:user_id>', methods=['POST'])
def approve_user(user_id):
    if not session.get('is_admin'):
        return jsonify({"error": "Acesso negado"}), 403
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_approved = TRUE WHERE id = %s", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Usuário aprovado!"})

@app.route('/admin/grant_vip/<int:user_id>', methods=['POST'])
def grant_vip(user_id):
    """Concede VIP por 30 dias e aprovação imediata."""
    if not session.get('is_admin'):
        return jsonify({"error": "Acesso negado"}), 403
        
    expiration = datetime.now() + timedelta(days=30)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_vip = TRUE, is_approved = TRUE, vip_expiration = %s WHERE id = %s", (expiration, user_id))
    conn.commit()
    conn.close()
    return jsonify({"message": "VIP concedido com sucesso (30 dias)!"})

@app.route('/admin/ban_user/<int:user_id>', methods=['POST'])
def ban_user(user_id):
    """Bane um usuário do sistema."""
    if not session.get('is_admin'):
        return jsonify({"error": "Acesso negado"}), 403
        
    # Proteção contra auto-banimento
    if user_id == session.get('user_id'):
        return jsonify({"error": "Você não pode banir a si mesmo!"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_banned = TRUE WHERE id = %s", (user_id,))
    conn.commit()
    conn.close()
    print(f"ADMIN: Usuário {user_id} foi BANIDO por {session.get('username')}")
    return jsonify({"message": "Usuário BANIDO com sucesso!"})

@app.route('/admin/unban_user/<int:user_id>', methods=['POST'])
def unban_user(user_id):
    """Remove o banimento de um usuário."""
    if not session.get('is_admin'):
        return jsonify({"error": "Acesso negado"}), 403
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_banned = FALSE WHERE id = %s", (user_id,))
    conn.commit()
    conn.close()
    print(f"ADMIN: Usuário {user_id} foi DESBANIDO por {session.get('username')}")
    return jsonify({"message": "Usuário desbanido!"})

@app.route('/admin/users', methods=['GET'])
def list_all_users():
    if not session.get('is_admin'):
        return jsonify({"error": "Acesso negado"}), 403
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, is_approved, quota_used, is_vip, vip_expiration, is_banned FROM users")
    users = cursor.fetchall()
    conn.close()
    return jsonify(users)

@app.route('/admin/tickets', methods=['GET'])
def list_support_tickets():
    """Lista conversas que foram escaladas pela IA para o Admin."""
    if not session.get('is_admin'):
        return jsonify({"error": "Acesso negado"}), 403
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Busca sessões que possuem pelo menos uma mensagem escalada
    # E traz o histórico recente dessas sessões
    cursor.execute("""
        SELECT sm.session_id, sm.sender, sm.message, sm.created_at, u.username 
        FROM support_messages sm
        LEFT JOIN users u ON sm.user_id = u.id
        WHERE sm.session_id IN (SELECT DISTINCT session_id FROM support_messages WHERE is_escalated = TRUE)
        ORDER BY sm.created_at DESC
    """)
    tickets = cursor.fetchall()
    conn.close()
    return jsonify(tickets)

@app.route('/support/history', methods=['GET'])
def support_history():
    """Retorna o histórico de mensagens da sessão atual (para polling)."""
    # Garante sessão
    if 'support_session_id' not in session:
        session['support_session_id'] = str(uuid.uuid4())
    support_sid = session['support_session_id']
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT sender, message, created_at FROM support_messages WHERE session_id = %s ORDER BY id ASC", (support_sid,))
    messages = cursor.fetchall()
    conn.close()
    return jsonify(messages)

@app.route('/admin/reply_ticket', methods=['POST'])
def admin_reply_ticket():
    if not session.get('is_admin'):
        return jsonify({"error": "Acesso negado"}), 403
    
    data = request.json
    session_id = data.get('session_id')
    message = data.get('message')
    
    conn = get_db_connection()
    cursor = conn.cursor()
    # Busca o user_id original da sessão para manter consistência
    cursor.execute("SELECT user_id FROM support_messages WHERE session_id = %s LIMIT 1", (session_id,))
    row = cursor.fetchone()
    target_user_id = row[0] if row else None
    
    cursor.execute("INSERT INTO support_messages (session_id, user_id, sender, message, is_escalated) VALUES (%s, %s, 'admin', %s, FALSE)", 
                   (session_id, target_user_id, message))
    conn.commit()
    conn.close()
    return jsonify({"message": "Resposta enviada!"})

@app.route('/admin/user_pass/<int:user_id>', methods=['GET', 'POST'])
def admin_user_pass(user_id):
    if not session.get('is_admin'):
        return jsonify({"error": "Acesso negado"}), 403
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    if request.method == 'GET':
        cursor.execute("SELECT password FROM users WHERE id = %s", (user_id,))
        res = cursor.fetchone()
        conn.close()
        return jsonify({"password": res['password']}) if res else jsonify({"error": "User not found"}), 404
        
    if request.method == 'POST':
        new_pass = request.json.get('password')
        cursor.execute("UPDATE users SET password = %s WHERE id = %s", (new_pass, user_id))
        conn.commit()
        conn.close()
        return jsonify({"message": "Senha alterada"})

@app.route('/admin/user_files/<int:user_id>', methods=['GET'])
def admin_user_files(user_id):
    if not session.get('is_admin'):
        return jsonify({"error": "Acesso negado"}), 403
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT hash, filename, size_bytes FROM arquivos_dolly WHERE owner_id = %s", (user_id,))
    files = cursor.fetchall()
    conn.close()
    return jsonify(files)

@app.route('/admin/delete_user/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    if not session.get('is_admin'):
        return jsonify({"error": "Acesso negado"}), 403
        
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # 1. Encontrar e deletar todos os arquivos desse usuário para liberar espaço
    cursor.execute("SELECT hash, filename FROM arquivos_dolly WHERE owner_id = %s", (user_id,))
    files = cursor.fetchall()
    
    for f in files:
        # Remove arquivos físicos
        # try:
        #     os.remove(os.path.join(UPLOAD_FOLDER, f['filename']))
        # except OSError:
        #     pass 
        pass
            
    # 2. Remove registros do banco
    cursor.execute("DELETE FROM file_chunks WHERE file_hash IN (SELECT hash FROM arquivos_dolly WHERE owner_id = %s)", (user_id,))
    cursor.execute("DELETE FROM arquivos_dolly WHERE owner_id = %s", (user_id,))
    cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Usuário e seus arquivos deletados!"})

@app.route('/admin/files', methods=['GET'])
def list_all_files():
    if not session.get('is_admin'):
        return jsonify({"error": "Acesso negado"}), 403
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT hash, filename, size_bytes, owner_id FROM arquivos_dolly")
    files = cursor.fetchall()
    conn.close()
    return jsonify(files)

def get_ai_status(filename, is_admin, is_vip, vt_status=None):
    """Define o status de segurança baseado no VirusTotal e regras simples."""
    # 1. Prioridade para o VirusTotal (Verificação Real)
    if vt_status == 'VIRUS':
        return "VIRUS (DETECTADO)"
    if vt_status == 'CLEAN':
        return "VERIFICADO (SEGURO)"

    # 2. Heurística (Fallback se VT for PENDING ou UNKNOWN)
    name_lower = filename.lower()
    suspicious_exts = ['.exe', '.bat', '.vbs', '.cmd', '.sh']
    suspicious_keywords = ['crack', 'keygen', 'hack', 'cheat', 'free_money']

    if is_admin:
        return "CRIADO PELA ADMINISTRAÇÃO"
    elif any(name_lower.endswith(ext) for ext in suspicious_exts) or any(kw in name_lower for kw in suspicious_keywords):
        return "VIRUS (SUSPEITO)"
    elif is_vip:
        return "CONFIAVEL"
    return "DESCONFIE"

@app.route('/dolly_store', methods=['GET'])
def dolly_store_feed():
    """Retorna todos os arquivos com análise de IA simulada."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    # Busca arquivos e info do dono (se é admin ou vip)
    cursor.execute("""
        SELECT f.hash, f.filename, f.size_bytes, f.created_at, f.vt_status, u.username, u.is_admin, u.is_vip 
        FROM arquivos_dolly f
        JOIN users u ON f.owner_id = u.id
        ORDER BY f.created_at DESC
    """)
    files = cursor.fetchall()
    conn.close()

    # Processamento da "IA"
    for f in files:
        f['ai_status'] = get_ai_status(f['filename'], f['is_admin'], f['is_vip'], f.get('vt_status'))
            
    return jsonify(files)

@app.route('/download_dolly/<file_hash>', methods=['GET'])
def download_dolly_file(file_hash):
    """Baixa o arquivo .dolly da loja, com verificação de segurança."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT f.filename, f.vt_status, u.is_admin, u.is_vip 
        FROM arquivos_dolly f
        JOIN users u ON f.owner_id = u.id
        WHERE f.hash = %s
    """, (file_hash,))
    file_data = cursor.fetchone()
    conn.close()

    if not file_data:
        return jsonify({"error": "Arquivo não encontrado"}), 404

    status = get_ai_status(file_data['filename'], file_data['is_admin'], file_data['is_vip'], file_data.get('vt_status'))
    
    # Se não for seguro e não tiver confirmação explícita
    if status not in ["CONFIAVEL", "CRIADO PELA ADMINISTRAÇÃO", "VERIFICADO (SEGURO)"]:
        if request.args.get('confirm') != 'true':
            return jsonify({
                "warning": True,
                "message": f"ATENÇÃO: Este arquivo é classificado como '{status}'. Tem certeza que deseja baixar o .dolly?",
                "status": status
            }), 400

    dolly_filename = f"{file_data['filename']}.dolly"
    try:
        return send_from_directory(DOLLY_FOLDER, dolly_filename, as_attachment=True)
    except FileNotFoundError:
        return jsonify({"error": "Arquivo .dolly físico não encontrado no servidor."}), 404

@app.route('/my_files', methods=['GET'])
def list_my_files():
    if 'user_id' not in session:
        return jsonify({"error": "Login necessário"}), 401
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT hash, filename, size_bytes FROM arquivos_dolly WHERE owner_id = %s", (session['user_id'],))
    files = cursor.fetchall()
    conn.close()
    return jsonify(files)

@app.route('/delete_file/<file_hash>', methods=['DELETE'])
def delete_file(file_hash):
    if 'user_id' not in session:
        return jsonify({"error": "Login necessário"}), 401
        
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Pega info do arquivo para descontar cota e saber nome
    cursor.execute("SELECT filename, size_bytes, owner_id FROM arquivos_dolly WHERE hash = %s", (file_hash,))
    file_data = cursor.fetchone()
    
    if file_data:
        # --- VERIFICAÇÃO DE SEGURANÇA ---
        # Se não for o dono E não for admin, bloqueia a exclusão
        if file_data['owner_id'] != session['user_id'] and not session.get('is_admin'):
            conn.close()
            return jsonify({"error": "Você não pode deletar arquivos de outros usuários!"}), 403

        # Remove físicos
        # try:
        #     os.remove(os.path.join(UPLOAD_FOLDER, file_data['filename']))
        # except OSError:
        #     pass
        pass
            
        # Atualiza cota do dono
        cursor.execute("UPDATE users SET quota_used = quota_used - %s WHERE id = %s", (file_data['size_bytes'], file_data['owner_id']))
        # Deleta registro
        cursor.execute("DELETE FROM arquivos_dolly WHERE hash = %s", (file_hash,))
        conn.commit()
        
    conn.close()
    return jsonify({"message": "Arquivo deletado!"})

def check_virustotal(file_hash):
    """Consulta a API do VirusTotal pelo hash do arquivo."""
    if not VIRUSTOTAL_API_KEY or "SUA_CHAVE" in VIRUSTOTAL_API_KEY:
        return "PENDING"
    
    url = f"https://www.virustotal.com/api/v3/files/{file_hash}"
    headers = {"x-apikey": VIRUSTOTAL_API_KEY}
    
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            stats = response.json().get('data', {}).get('attributes', {}).get('last_analysis_stats', {})
            if stats.get('malicious', 0) > 0:
                return "VIRUS"
            return "CLEAN"
        elif response.status_code == 404:
            return "UNKNOWN" # Arquivo nunca visto pelo VT
    except Exception as e:
        print(f"Erro VT: {e}")
    return "PENDING"

def finalize_file_processing(filename, user_id, conn, magnet_link=None):
    """
    Função centralizada para salvar metadados no banco e criar o arquivo .dolly
    """
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    # Garante o tamanho real do arquivo em disco
    file_size = os.path.getsize(file_path)
    file_hash = calculate_sha256(file_path)
    
    # Verifica VirusTotal
    vt_status = check_virustotal(file_hash)

    cursor = conn.cursor()
    # INSERT IGNORE evita erro se o arquivo já foi cadastrado antes
    # Nota: Passamos None para file_content pois usaremos a tabela de chunks para arquivos novos
    sql = "INSERT IGNORE INTO arquivos_dolly (hash, filename, size_bytes, owner_id, magnet_link, file_content, vt_status) VALUES (%s, %s, %s, %s, %s, %s, %s)"
    cursor.execute(sql, (file_hash, filename, file_size, user_id, magnet_link, None, vt_status))
    
    # Se o arquivo foi inserido agora (rowcount > 0), salvamos os chunks
    # Se rowcount == 0, o arquivo já existe, assumimos que os chunks também existem.
    if cursor.rowcount > 0:
        chunk_size = 2 * 1024 * 1024 # 2MB por pedaço (seguro para o limite de 6MB do TiDB)
        with open(file_path, 'rb') as f:
            chunk_index = 0
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                cursor.execute("INSERT INTO file_chunks (file_hash, chunk_index, chunk_data) VALUES (%s, %s, %s)", (file_hash, chunk_index, chunk))
                chunk_index += 1
    
    # Atualiza cota no banco
    cursor.execute("UPDATE users SET quota_used = quota_used + %s WHERE id = %s", (file_size, user_id))
    
    # Cria a estrutura do .dolly
    dolly_data = {
        "protocol": "dolly-v1",
        "original_name": filename,
        "size": file_size,
        "hash": file_hash,
        "download_endpoint": f"/baixar_conteudo/{filename}" 
    }
    if magnet_link:
        dolly_data['magnet_link'] = magnet_link
    
    # Salva o arquivo .dolly criptografado
    dolly_filename = f"{filename}.dolly"
    dolly_path = os.path.join(DOLLY_FOLDER, dolly_filename)
    json_str = json.dumps(dolly_data)
    encrypted_data = cipher_suite.encrypt(json_str.encode())
    
    with open(dolly_path, 'wb') as f:
        f.write(encrypted_data)
        
    # Opcional: Remover o arquivo do disco local já que está no banco (economiza espaço no Render)
    try:
        os.remove(file_path)
    except: pass

    return dolly_path

@app.route('/criar_dolly', methods=['POST'])
def create_dolly():
    """
    1. Recebe o arquivo real.
    2. Salva no servidor.
    3. Cria o arquivo de metadados .dolly.
    4. Retorna o arquivo .dolly para o usuário.
    """
    # Verifica Login e Aprovação
    if 'user_id' not in session:
        return jsonify({"error": "Faça login para criar arquivos"}), 401
    
    if not session.get('is_approved'):
        return jsonify({"error": "Sua conta ainda não foi aprovada pelo Admin"}), 403

    if 'file' not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Nome de arquivo inválido"}), 400

    # Verifica Cota
    # VIP: 1GB (1073741824 bytes) | Normal: 500MB (524288000 bytes)
    quota_limit = 1073741824 if session.get('is_vip') else 524288000
    limit_name = "1GB" if session.get('is_vip') else "500MB"

    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    if (session.get('quota_used', 0) + file_length) > quota_limit:
        return jsonify({"error": f"Cota de {limit_name} excedida!"}), 400
    file.seek(0) # Reseta ponteiro do arquivo

    filename = secure_filename(file.filename)
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    
    # Salva o arquivo original
    file.save(file_path)
    file_size = file_length # Define variável para uso na sessão
    
    # Calcula metadados
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        # Finaliza o processamento (DB, .dolly, cota)
        # A função finalize_file_processing agora retorna o .dolly path
        dolly_path = finalize_file_processing(filename, session['user_id'], conn)

        conn.commit()
        cursor.close()
        conn.close()
        
        # Atualiza sessão local
        session['quota_used'] += file_size
    
    if dolly_path:
        return send_file(dolly_path, as_attachment=True)
    else:
        # Isso pode acontecer se o arquivo já existir e o .dolly não for gerado novamente
        return jsonify({"message": "Arquivo já existe no sistema."}), 200

def download_torrent_and_create_dolly(magnet_link, user_id):
    """
    Função executada em background para baixar um torrent e criar o .dolly.
    """
    # 1. Configurar sessão do libtorrent
    ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})
    params = {'save_path': UPLOAD_FOLDER}
    handle = lt.add_magnet_uri(ses, magnet_link, params)
    ses.start_dht()

    print(f"Iniciando download do torrent para o usuário {user_id}...")

    # 2. Aguardar o download
    while not handle.status().is_seeding:
        s = handle.status()
        print(f'\rBaixando: {s.name} {s.progress * 100:.2f}% completo (vel: {s.download_rate / 1000:.1f} kB/s)', end='')
        time.sleep(1)
    
    print(f"\nDownload de '{handle.status().name}' completo!")
    
    # 3. Pós-processamento
    ti = handle.get_torrent_info()
    
    # Validação: Apenas torrents com UM arquivo são suportados por enquanto
    if ti.num_files() != 1:
        print(f"Erro: O torrent '{ti.name()}' contém {ti.num_files()} arquivos. Apenas torrents com um único arquivo são suportados. Abortando.")
        # Em uma implementação futura, você poderia deletar os arquivos baixados:
        # import shutil
        # shutil.rmtree(os.path.join(UPLOAD_FOLDER, ti.name()))
        return

    filename = secure_filename(ti.name())
    file_size = ti.total_size()

    # 4. Conectar ao DB e finalizar
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            # Verifica cota ANTES de inserir
            cursor.execute("SELECT quota_used FROM users WHERE id = %s", (user_id,))
            user = cursor.fetchone()
            
            # Verifica Cota (VIP vs Normal)
            # Precisamos saber se o usuário é VIP aqui, mas não temos session.
            # Vamos assumir 500MB para torrents em background por segurança ou consultar o banco
            # Para simplificar, mantemos 500MB hardcoded ou consultamos is_vip
            if (user['quota_used'] + file_size) > 524288000: # Mantendo conservador para background
                print(f"Erro de cota para usuário {user_id} ao baixar torrent. Excluindo arquivo.")
                os.remove(os.path.join(UPLOAD_FOLDER, filename))
                return

            # Finaliza o processamento (DB, .dolly, cota)
            finalize_file_processing(filename, user_id, conn, magnet_link=magnet_link)
            conn.commit()
        finally:
            conn.close()
    print(f"Processo de torrent para '{filename}' finalizado.")

@app.route('/add_magnet', methods=['POST'])
def add_magnet():
    """Recebe um link magnético e inicia o download em segundo plano."""
    # 1. Validação de sessão
    if 'user_id' not in session:
        return jsonify({"error": "Faça login para adicionar torrents"}), 401
    
    if not session.get('is_approved'):
        return jsonify({"error": "Sua conta ainda não foi aprovada pelo Admin"}), 403

    # 2. Validação do input
    data = request.json
    magnet_link = data.get('magnet_link')
    if not magnet_link or not magnet_link.startswith('magnet:'):
        return jsonify({"error": "Link magnético inválido"}), 400

    # 3. Iniciar download em background
    thread = threading.Thread(target=download_torrent_and_create_dolly, args=(magnet_link, session['user_id']))
    thread.daemon = True # Permite que o app principal saia mesmo que a thread esteja rodando
    thread.start()

    return jsonify({"message": "Download do torrent iniciado. O arquivo aparecerá em 'Meus Arquivos' quando concluído."})

@app.route('/status')
def status_check():
    """Verifica conectividade com o banco para a tela de intro."""
    conn = get_db_connection()
    if conn:
        conn.close()
        return jsonify({"status": "online", "database": "connected"})
    return jsonify({"error": "Database connection failed"}), 500

@app.route('/ler_dolly', methods=['POST'])
def read_dolly():
    """
    Recebe um arquivo .dolly, lê onde está o arquivo real e inicia o download.
    """
    if 'dolly_file' not in request.files:
        return jsonify({"error": "Envie um arquivo .dolly"}), 400
        
    dolly_file = request.files['dolly_file']
    
    try:
        # Lê e Descriptografa
        encrypted_content = dolly_file.read()
        decrypted_content = cipher_suite.decrypt(encrypted_content)
        metadata = json.loads(decrypted_content.decode())
        
        if metadata.get("protocol") != "dolly-v1":
            return jsonify({"error": "Arquivo .dolly inválido ou versão antiga"}), 400
            
        # Redireciona para a rota de download real
        # Nota: Na prática, o frontend usaria essa URL para baixar
        return jsonify({
            "message": "Arquivo localizado!",
            "file_info": metadata,
            "download_url": metadata['download_endpoint']
        })
        
    except Exception as e:
        return jsonify({"error": f"Erro ao processar .dolly: {str(e)}"}), 500

@app.route('/baixar_conteudo/<filename>')
def download_content(filename):
    """Rota que entrega o arquivo real (binário)."""
    # return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)
    
    # Agora busca do Banco de Dados
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT hash, file_content FROM arquivos_dolly WHERE filename = %s", (filename,))
        row = cursor.fetchone()
        
        if row:
            # 1. Tenta pegar do método antigo (coluna file_content)
            if row['file_content']:
                conn.close()
                return send_file(io.BytesIO(row['file_content']), as_attachment=True, download_name=filename)
            
            # 2. Se não tiver, tenta pegar dos chunks (método novo)
            file_hash = row['hash']
            cursor.execute("SELECT chunk_data FROM file_chunks WHERE file_hash = %s ORDER BY chunk_index", (file_hash,))
            chunks = cursor.fetchall()
            conn.close()
            
            if chunks:
                # Reconstrói o arquivo na memória
                combined_file = io.BytesIO()
                for chunk in chunks:
                    combined_file.write(chunk['chunk_data'])
                combined_file.seek(0)
                return send_file(combined_file, as_attachment=True, download_name=filename)
            
    return jsonify({"error": "Arquivo não encontrado no banco"}), 404

@app.route('/support/chat', methods=['POST'])
def support_chat():
    """Endpoint da IA de Suporte com 'Controle Total' via OpenAI (ChatGPT)."""
    data = request.json
    user_message = data.get('message', '')
    user_id = session.get('user_id')
    
    # Garante um ID de sessão para o chat (mesmo para anônimos)
    if 'support_session_id' not in session:
        session['support_session_id'] = str(uuid.uuid4())
    support_sid = session['support_session_id']
    
    # 1. Coleta de Contexto do Sistema (O que a IA "vê")
    system_info = {
        "db_status": "Desconectado (ALERTA)",
        "user_info": "Anônimo / Não Identificado",
        "files_count": "N/A"
    }
    
    conn = get_db_connection()
    if conn:
        system_info["db_status"] = "Conectado e Operacional (TiDB)"
        if user_id:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM arquivos_dolly WHERE owner_id = %s", (user_id,))
            count = cursor.fetchone()[0]
            system_info["files_count"] = str(count)
            system_info["user_info"] = f"Usuário: {session.get('username')} (ID: {user_id})"
            conn.close()
            
    # 2. Salva a mensagem do usuário e busca histórico (Memória)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Salva input do usuário
    cursor.execute("INSERT INTO support_messages (session_id, user_id, sender, message) VALUES (%s, %s, 'user', %s)", 
                   (support_sid, user_id, user_message))
    conn.commit()
    
    # Busca as últimas 10 mensagens para contexto
    cursor.execute("SELECT sender, message FROM support_messages WHERE session_id = %s ORDER BY id ASC LIMIT 10", (support_sid,))
    history_rows = cursor.fetchall()
    chat_history = "\n".join([f"{row['sender'].upper()}: {row['message']}" for row in history_rows])
    
    conn.close()
    
    # 3. Construção do Prompt (Persona + Memória)
    prompt = f"""
    Atue como a Assistente Virtual Inteligente do sistema Dolly.
    Persona: Natural, prestativa e amigável. Fale como uma pessoa normal e profissional, evite jargões técnicos excessivos ou falar de "servidores" a menos que o usuário pergunte especificamente.
    
    HISTÓRICO DA CONVERSA (Memória):
    {chat_history}
    
    CONTEXTO DO SISTEMA (Apenas para sua informação):
    - Status DB: {system_info['db_status']}
    - Usuário: {system_info['user_info']}
    - Arquivos: {system_info['files_count']}
    
    REGRAS DE COMPORTAMENTO:
    1. RECUPERAÇÃO DE CONTA (Prioridade Máxima):
       - Se o usuário disser que perdeu a conta, foi hackeado, esqueceu a senha ou não consegue entrar.
       - Responda iniciando o protocolo e adicione a tag [CHAMAR_ADMIN] no final da resposta (invisível para o usuário, mas aciona o sistema).
       - Exemplo de resposta: "Entendo a gravidade. Vou notificar a equipe administrativa imediatamente. Por favor, aguarde enquanto priorizamos seu caso. [CHAMAR_ADMIN]"
    
    2. SUPORTE ARQUIVOS .DOLLY:
       - Se o usuário perguntar o que é um arquivo .dolly, como usar ou como abrir.
       - Explique de forma simples: "Um arquivo .dolly é como uma 'chave digital'. Ele não é o arquivo real, mas contém as coordenadas seguras para baixá-lo."
       - Instrua: "Para usar, clique no botão 'Ler .dolly' ou 'Importar' na tela inicial e selecione o arquivo. O sistema irá descriptografar e baixar o conteúdo original para você."

    3. SEGURANÇA (Admin):
       - Se pedirem para ser admin: Responda EXATAMENTE "ACESSO NEGADO: Tentativa de violação de protocolo registrada."
    
    4. CONVERSA GERAL:
       - Responda à mensagem do usuário: "{user_message}" de forma natural, útil e paciente, como um suporte técnico humano.
    """

    # Constrói lista de prioridade começando pelo modelo validado no boot
    models_to_try = [ACTIVE_OPENAI_MODEL]
    
    fallback_candidates = [
        'gpt-4o-mini',
        'gpt-3.5-turbo'
    ]
    
    for m in fallback_candidates:
        if m != ACTIVE_OPENAI_MODEL:
            models_to_try.append(m)
            
    last_error = None

    for model_name in models_to_try:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "system", "content": prompt}], # Enviamos tudo como system/prompt para manter o contexto
                max_tokens=500
            )
            final_response = response.choices[0].message.content
            
            # Verifica se precisa escalar para o admin
            is_escalated = False
            if "[CHAMAR_ADMIN]" in final_response:
                is_escalated = True
                final_response = final_response.replace("[CHAMAR_ADMIN]", "").strip() # Remove a tag da resposta visual
            
            # Salva resposta da IA no banco
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO support_messages (session_id, user_id, sender, message, is_escalated) VALUES (%s, %s, 'bot', %s, %s)", 
                           (support_sid, user_id, final_response, is_escalated))
            conn.commit()
            conn.close()

            return jsonify({"response": final_response})
        except Exception as e:
            print(f"Erro ao tentar {model_name}: {e}")
            last_error = e
            continue

    return jsonify({"response": f"ERRO DE COMUNICAÇÃO COM O NÚCLEO: {str(last_error)}. Verifique a API Key."})

# Garante que o banco inicia mesmo usando Gunicorn (Render)
init_db()
select_best_openai_model() # Verifica qual IA está funcionando ao iniciar

if __name__ == '__main__':
    app.run(debug=True, port=5000)
