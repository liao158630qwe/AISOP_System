// Isolated browser checks: no manual preview server or fixed port required.
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const net=require('node:net');
const root=path.resolve(__dirname,'..');
async function freePort(){
  const socket=net.createServer();
  await new Promise((resolve,reject)=>{socket.once('error',reject);socket.listen(0,'127.0.0.1',resolve);});
  const port=socket.address().port;
  await new Promise(resolve=>socket.close(resolve));return port;
}
function run(script,env){
  return new Promise((resolve,reject)=>{
    const child=spawn(process.execPath,[path.join(__dirname,script)],{cwd:root,env,stdio:'inherit',windowsHide:true});
    child.once('error',reject);
    child.once('exit',code=>code===0?resolve():reject(new Error(`${script} exited with ${code}`)));
  });
}
(async()=>{
  const port=await freePort(),url=`http://127.0.0.1:${port}`;
  const output=path.join(root,'.runtime','ui-'+Date.now());fs.mkdirSync(output,{recursive:true});
  const env={...process.env,PORT:String(port),TEST_BASE_URL:url,TEST_OUTPUT_DIR:output};
  const server=spawn(process.execPath,['server.cjs'],{cwd:root,env,windowsHide:true,stdio:'ignore'});
  let spawnError;server.once('error',error=>{spawnError=error;});
  try{
    let ready=false;
    for(let i=0;i<50;i++){
      if(spawnError)throw spawnError;
      if(server.exitCode!==null)throw new Error('Preview server exited before startup');
      try{if((await fetch(url)).ok){ready=true;break;}}catch{}
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    if(!ready)throw new Error('Preview server did not become ready');
    const checks=['smoke-test.cjs','worker-test.cjs','ui-regression.cjs'];
    const selected=process.argv.length>2?process.argv.slice(2):checks;
    for(const script of selected){
      if(!checks.includes(script))throw new Error('Unknown browser check: '+script);
      await run(script,env);
    }
    console.log('Browser test artifacts: '+output);
  }finally{server.kill();}
})().catch(error=>{console.error(error);process.exitCode=1;});
