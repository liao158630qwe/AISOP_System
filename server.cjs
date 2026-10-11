const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const root = __dirname;
const types = {'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.mp4':'video/mp4','.webm':'video/webm','.jpg':'image/jpeg','.svg':'image/svg+xml','.json':'application/json; charset=utf-8','.csv':'text/csv; charset=utf-8'};
const allowed = new Set(['index.html','styles.css','app.js','server-ui.js']);
const server = http.createServer((req,res) => {
  let url;
  try { url = decodeURIComponent(new URL(req.url,'http://localhost').pathname); } catch { res.writeHead(400).end();return; }
  if(url==='/') url='/index.html';
  const relative=url.slice(1);
  const target=path.resolve(root,relative);
  const assetAllowed=relative.startsWith('assets/')&&target.startsWith(path.join(root,'assets')+path.sep);
  if(!target.startsWith(root+path.sep)||(!allowed.has(relative)&&!assetAllowed)) {res.writeHead(404).end('Not found');return;}
  fs.stat(target,(error,stat)=>{
    if(error||!stat.isFile()){res.writeHead(404).end('Not found');return;}
    const headers={'Content-Type':types[path.extname(target)]||'application/octet-stream','Accept-Ranges':'bytes','Cache-Control':'no-cache'};
    const range=req.headers.range;
    if(range){
      const match=/^bytes=(\d*)-(\d*)$/.exec(range);
      if(!match){res.writeHead(416,{'Content-Range':`bytes */${stat.size}`}).end();return;}
      const start=match[1]?Number(match[1]):Math.max(0,stat.size-Number(match[2]));
      const end=match[1]&&match[2]?Math.min(Number(match[2]),stat.size-1):stat.size-1;
      if(start>end||start>=stat.size){res.writeHead(416,{'Content-Range':`bytes */${stat.size}`}).end();return;}
      res.writeHead(206,{...headers,'Content-Range':`bytes ${start}-${end}/${stat.size}`,'Content-Length':end-start+1});
      fs.createReadStream(target,{start,end}).pipe(res);
    }else{res.writeHead(200,{...headers,'Content-Length':stat.size});fs.createReadStream(target).pipe(res);}
  });
});
server.listen(Number(process.env.PORT)||8080,'127.0.0.1',()=>console.log(`AI-SOP ready at http://localhost:${server.address().port}`));
