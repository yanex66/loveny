import os, glob, re
svg_icon = '&#x1FA99;'

files_changed = 0
for f in glob.glob('dating/templates/**/*.html', recursive=True):
    with open(f, 'r', encoding='utf-8', errors='ignore') as file:
        content = file.read()
    
    new_content = re.sub(r'<span class="text-sm">[^<]{1,4}</span>\s*<span id="nav-coin-balance">', f'<span class="text-sm">{svg_icon}</span>\n                <span id="nav-coin-balance">', content)
    new_content = re.sub(r'<span class="text-sm">[^<]{1,4}</span>\s*<span id="me-header-coin-balance"', f'<span class="text-sm">{svg_icon}</span>\n                <span id="me-header-coin-balance"', new_content)
    new_content = re.sub(r'<span class="text-sm">[^<]{1,4}</span>\s*<span id="settings-header-coin-balance"', f'<span class="text-sm">{svg_icon}</span>\n                <span id="settings-header-coin-balance"', new_content)
    new_content = re.sub(r'<span class="text-sm">[^<]{1,4}</span>\s*<span>{{\s*user_coin_balance', f'<span class="text-sm">{svg_icon}</span>\n                    <span>{{{{ user_coin_balance', new_content)
    
    # Catch any generic next to {{ user_coin_balance
    new_content = re.sub(r'<span class="text-sm">[^<]{1,4}</span>(\s*)<span>{{\s*user_coin_balance', f'<span class="text-sm">{svg_icon}</span>\\1<span>{{{{ user_coin_balance', new_content)

    new_content = re.sub(r'<span class="text-sm">[^<]{1,4}</span>\s*<span id="hub-coins-val">', f'<span class="text-sm">{svg_icon}</span>\n                    <span id="hub-coins-val">', new_content)
    new_content = re.sub(r'<span class="text-sm">[^<]{1,4}</span>\s*<span id="header-coin-balance">', f'<span class="text-sm">{svg_icon}</span>\n                    <span id="header-coin-balance">', new_content)

    new_content = new_content.replace('?? <strong>Note:', '&#x1FA99; <strong>Note:')
    new_content = new_content.replace('costs ?{{', 'costs &#x1FA99;{{')

    new_content = new_content.replace('<span class="text-3xl">??</span>', '<span class="text-3xl">&#x1F512;</span>')
    new_content = new_content.replace('?? Refresh', '&#x1F504; Refresh')

    if new_content != content:
        with open(f, 'w', encoding='utf-8') as file:
            file.write(new_content)
        files_changed += 1
        print(f'Fixed {f}')
print(f'Done. Changed {files_changed} files.')

