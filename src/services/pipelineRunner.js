const { spawn } = require('child_process');
const path = require('path');
const { uvEnv, uvCommand } = require('./uvPython');

const PIPELINE_DIR = path.join(__dirname, '..', '..', 'pipeline');

function runPythonScript(scriptName, args = []) {
  return new Promise((resolve, reject) => {
    const scriptPath = path.join(PIPELINE_DIR, scriptName);
    const { cmd, args: uvArgs } = uvCommand([scriptPath, ...args]);
    const proc = spawn(cmd, uvArgs, {
      cwd: PIPELINE_DIR,
      env: uvEnv(),
      timeout: 120000,
    });

    let stdout = '';
    let stderr = '';

    proc.stdout.on('data', (data) => {
      stdout += data.toString();
    });

    proc.stderr.on('data', (data) => {
      stderr += data.toString();
    });

    proc.on('close', (code) => {
      if (code === 0) {
        resolve(stdout.trim());
      } else {
        reject(new Error(`Python script exited with code ${code}: ${stderr}`));
      }
    });

    proc.on('error', (err) => {
      reject(new Error(`Failed to start Python: ${err.message}`));
    });
  });
}

module.exports = { runPythonScript };
