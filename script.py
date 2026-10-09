import re

files = [
    'dating/templates/dating/hookup/discovery.html',
    'dating/templates/dating/hookup/chat_room.html',
    'dating/templates/dating/sex_call_hub.html'
]

for file in files:
    with open(file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if '{% block floating_coin_pill %}' not in content:
        content = content.replace('{% block content %}', '{% block content %}\n{% block floating_coin_pill %}{% endblock %}')
        with open(file, 'w', encoding='utf-8') as f:
            f.write(content)
