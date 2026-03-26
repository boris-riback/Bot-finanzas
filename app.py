import os
import json
import re
import requests as req
from datetime import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

app = Flask(__name__)

# CONFIGURACION
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
GDRIVE_FOLDER_ID = "1FGAKwSkzj-eU-wB__k3WaVE4y0CnpQWx"
NUMEROS_AUTORIZADOS = os.environ.get("NUMEROS_AUTORIZADOS", "").split(",")

def get_drive_service():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def subir_a_drive(nombre_archivo, contenido):
    service = get_drive_service()
    media = MediaInMemoryUpload(contenido.encode("utf-8"), mimetype="text/plain")
    file_metadata = {
        "name": nombre_archivo,
        "parents": [GDRIVE_FOLDER_ID]
    }
    service.files().create(body=file_metadata, media_body=media).execute()

def interpretar_mensaje(mensaje, remitente):
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    prompt = f"""Sos un asistente financiero para un negocio gastronomico en Argentina (bar, fabrica de cerveza, beer truck).
Tu tarea es interpretar mensajes de WhatsApp y extraer datos financieros.
El mensaje puede ser:
1. Un EGRESO (pago a proveedor): extraer proveedor, monto, medio de pago, concepto, fecha
2. Un INGRESO: extraer concepto, monto, medio de cobro, fecha
3. Un COMANDO: /saldo, /reporte, /pendientes, /ayuda
Medios de pago validos: Efectivo, Transferencia, MercadoPago, Echeq
Responde SOLO en JSON con esta estructura:
{{
  "tipo": "egreso" | "ingreso" | "comando" | "error",
  "comando": "/saldo" | "/reporte" | "/pendientes" | "/ayuda" | null,
  "proveedor": "nombre" | null,
  "concepto": "descripcion" | null,
  "monto": numero | null,
  "medio_pago": "Efectivo" | "Transferencia" | "MercadoPago" | "Echeq" | null,
  "fecha": "dd-mm-yyyy" | null,
  "es_interno": true | false,
  "error": "descripcion del error" | null,
  "confirmacion": "texto de confirmacion para el usuario"
}}
Fecha de hoy: {datetime.now().strftime('%d-%m-%Y')}
Mensaje: {mensaje}
Remitente: {remitente}"""

    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}]
    }
    response = req.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
    texto = response.json()["content"][0]["text"]
    texto = re.sub(r'```json|```', '', texto).strip()
    return json.loads(texto)

def generar_archivo_registro(datos, remitente):
    fecha = datos.get("fecha") or datetime.now().strftime("%d-%m-%Y")
    ahora = datetime.now().strftime("%d-%m-%Y %H:%M")
    if datos["tipo"] == "egreso":
        contenido = f"""TIPO: EGRESO
FECHA: {fecha}
PROVEEDOR: {datos.get('proveedor', 'Sin especificar')}
CONCEPTO: {datos.get('concepto', 'Sin especificar')}
MONTO: ${datos.get('monto', 0):,.2f}
MEDIO DE PAGO: {datos.get('medio_pago', 'Sin especificar')}
REGISTRADO POR: {remitente}
REGISTRADO EL: {ahora}
FUENTE: WhatsApp Bot
"""
        nombre = f"EGRESO_{datos.get('proveedor','sin_proveedor').replace(' ','_')}_{fecha.replace('-','')}.txt"
    elif datos["tipo"] == "ingreso":
        contenido = f"""TIPO: INGRESO
FECHA: {fecha}
CONCEPTO: {datos.get('concepto', 'Sin especificar')}
MONTO: ${datos.get('monto', 0):,.2f}
MEDIO DE COBRO: {datos.get('medio_pago', 'Sin especificar')}
REGISTRADO POR: {remitente}
REGISTRADO EL: {ahora}
FUENTE: WhatsApp Bot
"""
        nombre = f"INGRESO_{datos.get('concepto','sin_concepto').replace(' ','_')}_{fecha.replace('-','')}.txt"
    else:
        contenido = f"MENSAJE NO CLASIFICADO: {datos}\nREGISTRADO EL: {ahora}\n"
        nombre = f"PENDIENTE_{fecha.replace('-','')}.txt"
    return nombre, contenido

def respuesta_comando(comando):
    respuestas = {
        "/ayuda": """Comandos disponibles:
- /saldo: Saldo actual de cajas
- /reporte: Resumen del mes
- /pendientes: Cheques y facturas por vencer

Para registrar un egreso:
MGB 5000 efectivo
o: egreso - MGB - 5000 - transferencia

Para registrar un ingreso:
ingreso - servicio del dia - 50000 - nave""",
        "/saldo": "Para consultar el saldo actual, Claude lo procesara en el proximo ciclo de Cowork.",
        "/reporte": "Para generar el reporte mensual, Claude lo procesara en el proximo ciclo de Cowork.",
        "/pendientes": "Para ver los pendientes, Claude los revisara en el proximo ciclo de Cowork."
    }
    return respuestas.get(comando, "Comando no reconocido. Escribi /ayuda para ver los comandos disponibles.")

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.values.get("Body", "").strip()
    remitente = request.values.get("From", "").replace("whatsapp:", "")
    resp = MessagingResponse()
    msg = resp.message()
    if NUMEROS_AUTORIZADOS and remitente not in NUMEROS_AUTORIZADOS:
        msg.body("No estas autorizado para usar este bot.")
        return str(resp)
    if not incoming_msg:
        msg.body("Mensaje vacio. Escribi /ayuda para ver como usar el bot.")
        return str(resp)
    try:
        datos = interpretar_mensaje(incoming_msg, remitente)
        if datos["tipo"] == "comando":
            msg.body(respuesta_comando(datos.get("comando", "/ayuda")))
            return str(resp)
        if datos.get("es_interno"):
            msg.body("Transferencia interna detectada - no se registra.")
            return str(resp)
        if datos["tipo"] == "error":
            msg.body(f"No pude interpretar el mensaje: {datos.get('error', 'dato faltante')}.\nEscribi /ayuda para ver el formato correcto.")
            return str(resp)
        nombre_archivo, contenido = generar_archivo_registro(datos, remitente)
        subir_a_drive(nombre_archivo, contenido)
        emoji = "💸" if datos["tipo"] == "egreso" else "💰"
        confirmacion = datos.get("confirmacion") or "Registrado correctamente."
        msg.body(f"{emoji} {confirmacion}\n\nGuardado en carpeta Para Claude.")
    except json.JSONDecodeError:
        msg.body("Error interpretando el mensaje. Intenta de nuevo o escribi /ayuda.")
    except Exception as e:
        msg.body(f"Error inesperado: {str(e)[:150]}")
    return str(resp)

@app.route("/", methods=["GET"])
def health():
    return "Bot Finanzas OK", 200

if __name__ == "__main__":
    app.run(debug=False, port=5000)
