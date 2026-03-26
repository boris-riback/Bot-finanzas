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

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
GDRIVE_FOLDER_ID = "1dnw0pgk4JeXLENzpGgUYV4a_IoUtxPrU"
NUMEROS_AUTORIZADOS = os.environ.get("NUMEROS_AUTORIZADOS", "").split(",")

MIME_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
}


def get_drive_service():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)


def subir_texto_a_drive(nombre_archivo, contenido):
    service = get_drive_service()
    media = MediaInMemoryUpload(contenido.encode("utf-8"), mimetype="text/plain")
    file_metadata = {
        "name": nombre_archivo,
        "parents": [GDRIVE_FOLDER_ID]
    }
    service.files().create(
        body=file_metadata,
        media_body=media,
        supportsAllDrives=True,
        fields="id"
    ).execute()


def subir_binario_a_drive(nombre_archivo, contenido_bytes, mimetype):
    service = get_drive_service()
    media = MediaInMemoryUpload(contenido_bytes, mimetype=mimetype)
    file_metadata = {
        "name": nombre_archivo,
        "parents": [GDRIVE_FOLDER_ID]
    }
    service.files().create(
        body=file_metadata,
        media_body=media,
        supportsAllDrives=True,
        fields="id"
    ).execute()


def descargar_archivo_twilio(url):
    response = req.get(
        url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    )
    return response.content, response.headers.get("Content-Type", "application/octet-stream")


def interpretar_mensaje(mensaje, remitente):
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    prompt = (
        "Sos un asistente financiero para un negocio gastronomico en Argentina "
        "(bar, fabrica de cerveza artesanal, beer truck para eventos).\n\n"
        "REGLAS IMPORTANTES:\n"
        "- Una transferencia interna es cuando el dinero se mueve entre cuentas PROPIAS del negocio (ej: de Galicia a MercadoPago)\n"
        "- Si el mensaje menciona un proveedor con un monto y medio de pago, SIEMPRE es un EGRESO, nunca interno\n"
        "- Si no hay medio de pago mencionado, asumir Efectivo por defecto\n"
        "- Comandos validos: /saldo, /reporte, /pendientes, /ayuda\n\n"
        "Interpreta el mensaje y devuelve SOLO JSON con esta estructura:\n"
        "{\n"
        '  "tipo": "egreso" o "ingreso" o "comando" o "error",\n'
        '  "comando": "/saldo" o "/reporte" o "/pendientes" o "/ayuda" o null,\n'
        '  "proveedor": "nombre" o null,\n'
        '  "concepto": "descripcion" o null,\n'
        '  "monto": numero o null,\n'
        '  "medio_pago": "Efectivo" o "Transferencia" o "MercadoPago" o "Echeq" o null,\n'
        '  "fecha": "dd-mm-yyyy" o null,\n'
        '  "es_interno": true o false,\n'
        '  "error": "descripcion" o null,\n'
        '  "confirmacion": "texto confirmacion"\n'
        "}\n\n"
        f"Fecha de hoy: {datetime.now().strftime('%d-%m-%Y')}\n"
        f"Mensaje: {mensaje}\n"
        f"Remitente: {remitente}"
    )
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}]
    }
    response = req.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json=body
    )
    response_json = response.json()
    if "content" not in response_json:
        raise Exception(f"API error: {response_json}")
    texto = response_json["content"][0]["text"]
    texto = re.sub(r'```json|```', '', texto).strip()
    return json.loads(texto)


def generar_archivo_registro(datos, remitente):
    fecha = datos.get("fecha") or datetime.now().strftime("%d-%m-%Y")
    ahora = datetime.now().strftime("%d-%m-%Y %H:%M")
    if datos["tipo"] == "egreso":
        contenido = (
            "TIPO: EGRESO\n"
            f"FECHA: {fecha}\n"
            f"PROVEEDOR: {datos.get('proveedor', 'Sin especificar')}\n"
            f"CONCEPTO: {datos.get('concepto', 'Sin especificar')}\n"
            f"MONTO: ${datos.get('monto', 0):,.2f}\n"
            f"MEDIO DE PAGO: {datos.get('medio_pago', 'Sin especificar')}\n"
            f"REGISTRADO POR: {remitente}\n"
            f"REGISTRADO EL: {ahora}\n"
            "FUENTE: WhatsApp Bot\n"
        )
        nombre = f"EGRESO_{datos.get('proveedor','sin_proveedor').replace(' ','_')}_{fecha.replace('-','')}.txt"
    elif datos["tipo"] == "ingreso":
        contenido = (
            "TIPO: INGRESO\n"
            f"FECHA: {fecha}\n"
            f"CONCEPTO: {datos.get('concepto', 'Sin especificar')}\n"
            f"MONTO: ${datos.get('monto', 0):,.2f}\n"
            f"MEDIO DE COBRO: {datos.get('medio_pago', 'Sin especificar')}\n"
            f"REGISTRADO POR: {remitente}\n"
            f"REGISTRADO EL: {ahora}\n"
            "FUENTE: WhatsApp Bot\n"
        )
        nombre = f"INGRESO_{datos.get('concepto','sin_concepto').replace(' ','_')}_{fecha.replace('-','')}.txt"
    else:
        contenido = f"MENSAJE NO CLASIFICADO: {datos}\nREGISTRADO EL: {ahora}\n"
        nombre = f"PENDIENTE_{fecha.replace('-','')}.txt"
    return nombre, contenido


def respuesta_comando(comando):
    respuestas = {
        "/ayuda": (
            "Comandos disponibles:\n"
            "- /saldo: Saldo actual de cajas\n"
            "- /reporte: Resumen del mes\n"
            "- /pendientes: Cheques y facturas por vencer\n\n"
            "Para registrar un egreso:\n"
            "MGB 5000 efectivo\n"
            "o: egreso - MGB - 5000 - transferencia\n\n"
            "Para registrar un ingreso:\n"
            "ingreso - servicio del dia - 50000 - nave\n\n"
            "Para enviar archivos:\n"
            "Manda la foto o PDF directamente"
        ),
        "/saldo": "Para consultar el saldo, Claude lo procesara en el proximo ciclo de Cowork.",
        "/reporte": "Para generar el reporte, Claude lo procesara en el proximo ciclo de Cowork.",
        "/pendientes": "Para ver los pendientes, Claude los revisara en el proximo ciclo de Cowork."
    }
    return respuestas.get(comando, "Comando no reconocido. Escribi /ayuda.")


@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.values.get("Body", "").strip()
    remitente = request.values.get("From", "").replace("whatsapp:", "")
    num_media = int(request.values.get("NumMedia", 0))

    resp = MessagingResponse()
    msg = resp.message()

    if NUMEROS_AUTORIZADOS and remitente not in NUMEROS_AUTORIZADOS:
        msg.body("No estas autorizado para usar este bot.")
        return str(resp)

    try:
        # CASO 1: Hay archivos adjuntos
        if num_media > 0:
            archivos_guardados = []
            ahora = datetime.now().strftime("%d%m%Y_%H%M%S")

            for i in range(num_media):
                media_url = request.values.get(f"MediaUrl{i}")
                media_type = request.values.get(f"MediaContentType{i}", "")

                # Determinar extensión
                if "pdf" in media_type:
                    ext = "pdf"
                elif "jpeg" in media_type or "jpg" in media_type:
                    ext = "jpg"
                elif "png" in media_type:
                    ext = "png"
                else:
                    ext = "bin"

                nombre_archivo = f"ARCHIVO_{ahora}_{i+1}.{ext}"

                # Si hay texto adjunto al archivo, incluirlo como nombre descriptivo
                if incoming_msg:
                    texto_limpio = incoming_msg[:30].replace(" ", "_").replace("/", "-")
                    nombre_archivo = f"ARCHIVO_{texto_limpio}_{ahora}_{i+1}.{ext}"

                # Descargar y subir a Drive
                contenido_bytes, mimetype = descargar_archivo_twilio(media_url)
                subir_binario_a_drive(nombre_archivo, contenido_bytes, mimetype)
                archivos_guardados.append(nombre_archivo)

            msg.body(f"📁 {len(archivos_guardados)} archivo(s) guardado(s) en carpeta Para Claude:\n" + "\n".join(archivos_guardados))
            return str(resp)

        # CASO 2: Solo texto
        if not incoming_msg:
            msg.body("Mensaje vacio. Escribi /ayuda.")
            return str(resp)

        datos = interpretar_mensaje(incoming_msg, remitente)

        if datos["tipo"] == "comando":
            msg.body(respuesta_comando(datos.get("comando", "/ayuda")))
            return str(resp)

        if datos.get("es_interno"):
            msg.body("Transferencia interna - no se registra.")
            return str(resp)

        if datos["tipo"] == "error":
            msg.body(f"No pude interpretar: {datos.get('error', 'dato faltante')}. Escribi /ayuda.")
            return str(resp)

        nombre_archivo, contenido = generar_archivo_registro(datos, remitente)
        subir_texto_a_drive(nombre_archivo, contenido)
        emoji = "💸" if datos["tipo"] == "egreso" else "💰"
        confirmacion = datos.get("confirmacion") or "Registrado correctamente."
        msg.body(f"{emoji} {confirmacion}\n\nGuardado en carpeta Para Claude.")

    except json.JSONDecodeError:
        msg.body("Error interpretando el mensaje. Intenta de nuevo o escribi /ayuda.")
    except Exception as e:
        msg.body(f"Error: {str(e)[:500]}")

    return str(resp)


@app.route("/", methods=["GET"])
def health():
    return "Bot Finanzas OK", 200


if __name__ == "__main__":
    app.run(debug=False, port=5000)
