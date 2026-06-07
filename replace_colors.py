import os

def replace_in_file(filepath, replacements):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    for old, new in replacements:
        content = content.replace(old, new)
        
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

replacements_root = [
    ('--gold:', '--primary:'),
    ('--gold-light:', '--primary-light:'),
    ('--gold-dark:', '--primary-dark:'),
    ('--border-gold:', '--border-primary:'),
    ('var(--gold)', 'var(--primary)'),
    ('var(--gold-light)', 'var(--primary-light)'),
    ('var(--gold-dark)', 'var(--primary-dark)'),
    ('var(--border-gold)', 'var(--border-primary)'),
    ('#b8860b', '#2563eb'),
    ('#daa520', '#3b82f6'),
    ('#8b6508', '#1d4ed8'),
    ('rgba(184,134,11,0.25)', 'rgba(37,99,235,0.25)'),
    ('rgba(184, 134, 11, 0.25)', 'rgba(37, 99, 235, 0.25)')
]

templates_dir = r"c:\Users\aardi\Desktop\mom\Dev Laxmi Suppliers\templates"
for filename in os.listdir(templates_dir):
    if filename.endswith(".html"):
        filepath = os.path.join(templates_dir, filename)
        replace_in_file(filepath, replacements_root)
        print(f"Updated {filename}")
