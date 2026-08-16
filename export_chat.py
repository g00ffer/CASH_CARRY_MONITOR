import json
from datetime import datetime

# Укажите имя вашего JSON файла
input_file = 'chat-export-1786803203303.json'
output_file = 'cash-carry-monitor-full-chat_5.md'

with open(input_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

# JSON может быть списком или словарем (в зависимости от способа экспорта)
if isinstance(data, list):
    data = data[0]

messages = data.get('chat', {}).get('history', {}).get('messages', {})
# Сортируем по timestamp, чтобы восстановить хронологию
sorted_messages = sorted(messages.values(), key=lambda x: x.get('timestamp', 0))

with open(output_file, 'w', encoding='utf-8') as out_f:
    out_f.write(f"# 🚀 Cash-and-Carry Monitor (Stage 1 MVP)\n\n")
    out_f.write(f"*Полная история проектирования архитектуры и написания кода.*\n")
    out_f.write(f"*Дата генерации: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n---\n\n")
    
    for msg in sorted_messages:
        role = msg.get('role', 'unknown').capitalize()
        
        # Форматируем заголовки ролей
        if role == 'User':
            role_header = '### 👤 **User**'
        elif role == 'Assistant':
            role_header = '### 🤖 **Assistant**'
        else:
            continue
            
        content = ""
        # Пользовательские сообщения
        if msg.get('content'):
            content = msg['content']
        # Сообщения ассистента (текст лежит в content_list с phase="answer")
        elif msg.get('content_list'):
            for item in msg['content_list']:
                if item.get('phase') == 'answer' and item.get('content'):
                    content += item['content'] + "\n\n"
        
        # Пропускаем пустые или сугубо технические сообщения
        if not content.strip():
            continue 
            
        out_f.write(f"{role_header}\n\n")
        out_f.write(f"{content.strip()}\n\n")
        out_f.write("---\n\n")

print(f"✅ Готово! Файл успешно сохранен как '{output_file}'")