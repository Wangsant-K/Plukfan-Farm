# =============================================================================
# secrets.example.py — แม่แบบของ secrets.py (ไฟล์นี้ commit ขึ้น repo ได้)
# -----------------------------------------------------------------------------
# วิธีใช้:
#   1) คัดลอกไฟล์นี้เป็น secrets.py   (cp secrets.example.py secrets.py)
#   2) กรอกค่าจริงใน secrets.py
#   3) secrets.py ถูก gitignore อยู่แล้ว → ค่าลับจริงจะไม่ถูก commit
# =============================================================================

# ----- WiFi -----
WIFI_SSID = "your_wifi_ssid"
WIFI_PASS = "your_wifi_password"

# ----- MQTT broker -----
MQTT_HOST = "mqtt.example.com"
MQTT_USER = "your_mqtt_user"
MQTT_PASS = "your_mqtt_password"

# CA cert (PEM bytes) สำหรับ verify broker ถ้าใช้ self-signed; None = ใช้ default
MQTT_SSL_CA = None
