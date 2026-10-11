// Select an explicit interpreter, otherwise the project's existing Anaconda environment.
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const python=process.env.PYTHON||(process.platform==='win32'&&fs.existsSync('D:/Anaconda/python.exe')?'D:/Anaconda/python.exe':'python');
const child=spawn(python,process.argv.slice(2),{stdio:'inherit',windowsHide:true});
child.on('error',error=>{console.error(error.message);process.exitCode=1;});
child.on('exit',code=>{process.exitCode=code??1;});
