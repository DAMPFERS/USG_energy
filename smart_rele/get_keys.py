import tinytuya
import json

# Включаем подробный вывод отладки
tinytuya.set_debug(True)

# Задаем ваши явные параметры облака
CLOUD_REGION = "eu"  # Центральная Европа, где подтверждена подписка
API_KEY = "utkc4fr4efc55vedmc3c"
API_SECRET = "dd2adef7dabb49648a51c4bef8e0ed71"
DEVICE_ID = "bf300250be9bf94b97w0by" # Любой рабочий ID для проверки пула

print("Стучимся напрямую в Tuya Cloud...")
try:
    # Инициализируем прямое подключение к облаку API
    c = tinytuya.Cloud(
        apiRegion=CLOUD_REGION,
        apiKey=API_KEY,
        apiSecret=API_SECRET,
        apiDeviceID=DEVICE_ID
    )

    # Запрашиваем полный список устройств, привязанных к API
    devices = c.getdevices()
    
    print("\n--- ОТВЕТ СЕРВЕРА TUYA ---")
    if isinstance(devices, dict) and "result" in devices:
        print(f"Успешно! Найдено устройств: {len(devices['result'])}")
        
        # Красиво выводим имя, ID и локальный ключ каждого устройства
        for dev in devices['result']:
            print(f"\nИмя: {dev.get('name')}")
            print(f"ID устройства (dev_id): {dev.get('id')}")
            print(f"Локальный ключ (local_key): {dev.get('local_key')}")
            
        # Сохраняем результат в файл для удобства
        with open("tuya_extracted_keys.json", "w", encoding="utf-8") as f:
            json.dump(devices, f, indent=4, ensure_ascii=False)
        print("\nПолный лог сохранен в файл: tuya_extracted_keys.json")
    else:
        print("Не удалось прочитать список устройств. Сырой ответ:")
        print(devices)

except Exception as e:
    print(f"Произошла ошибка выполнения скрипта: {e}")
