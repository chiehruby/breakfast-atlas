from pathlib import Path
p=Path(__file__).parent
app=(p/'app.js').read_text(encoding='utf-8')
markers=[app.find(marker) for marker in ['const L=', 'const summaries='] if app.find(marker)>=0]
app=app[:min(markers)]+(p/'visual-chapters.js').read_text(encoding='utf-8')
(p/'app.js').write_text(app,encoding='utf-8')
index=(p/'index.html').read_text(encoding='utf-8').replace('<script src="proposal-data.js"></script>','')
(p/'index.html').write_text(index,encoding='utf-8')
html=index.replace('<link rel="stylesheet" href="style.css">','<style>'+(p/'style.css').read_text(encoding='utf-8')+'</style>')
html=html.replace('<script src="app.js"></script>','<script>'+app.replace('</script','<\\/script')+'</script>')
(p/'Breakfast Atlas.html').write_text(html,encoding='utf-8')
print('Built v0.3 visual proposal; no original-text section or embedded source dataset.')
