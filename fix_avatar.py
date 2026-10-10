import re
with open('dating/templates/dating/profile_detail.html', 'r', encoding='utf-8') as f:
    content = f.read()
content = content.replace('profile.user.username|slice:\":1\"|upper', 'profile.user.first_name|default:profile.user.username|slice:\":1\"|upper')
with open('dating/templates/dating/profile_detail.html', 'w', encoding='utf-8') as f:
    f.write(content)
