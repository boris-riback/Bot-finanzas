# Bot Finanzas — WhatsApp + Google Drive

## Variables de entorno (configurar en Render)

| Variable | Valor |
|---|---|
| `TWILIO_ACCOUNT_SID` | Tu Account SID de Twilio |
| `TWILIO_AUTH_TOKEN` | Tu Auth Token de Twilio |
| `TWILIO_WHATSAPP_NUMBER` | Número de Twilio (ej: +14155238886) |
| `ANTHROPIC_API_KEY` | Tu API Key de Anthropic |
| `GOOGLE_CREDENTIALS_JSON` | Contenido completo del archivo JSON de Google |
| `NUMEROS_AUTORIZADOS` | Números separados por coma (ej: +5493534000000,+5493534111111) |

## Formato de mensajes

### Registrar egreso
```
MGB 5000 efectivo
MGB - 5000 - efectivo
egreso - MGB - 5000 - transferencia - insumos
```

### Registrar ingreso
```
ingreso servicio del dia 50000 nave
ingreso - alquiler chopera - 110000 - mercadopago
```

### Comandos
```
/saldo
/reporte
/pendientes
/ayuda
```

## Deploy en Render

1. Subir estos archivos a un repositorio GitHub
2. En Render: New Web Service → conectar repo
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn app:app`
5. Agregar las variables de entorno
6. Deploy

## Configurar webhook en Twilio

Una vez deployado, copiar la URL de Render y pegarla en:
Twilio Console → Messaging → WhatsApp Sandbox → 
"When a message comes in": `https://tu-app.onrender.com/webhook`
