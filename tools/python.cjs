// Share interpreter selection across npm commands, local startup and browser E2E.
const {spawn,spawnSync}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
function resolvePython(){
  if(process.env.PYTHON)return process.env.PYTHON;
  const local=path.resolve(__dirname,'..','.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
  // A moved/deleted base interpreter can leave an unusable Windows venv
  // launcher behind. Probe it before selecting it over a working fallback.
  if(fs.existsSync(local)&&spawnSync(local,['--version'],{stdio:'ignore',windowsHide:true,timeout:5000}).status===0)return local;
  if(process.platform==='win32'&&fs.existsSync('D:/Anaconda/python.exe'))return 'D:/Anaconda/python.exe';
  return process.platform==='win32'?'python':'python3';
}
module.exports={resolvePython};
if(require.main===module){
  const child=spawn(resolvePython(),process.argv.slice(2),{stdio:'inherit',windowsHide:true});
  child.on('error',error=>{console.error('无法启动 Python：'+error.message);process.exitCode=1;});
  child.on('exit',code=>{process.exitCode=code??1;});
}
