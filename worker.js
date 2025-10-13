<<<<<<< HEAD
// worker.js (improved)
=======
>>>>>>> a8b0cf8 (Initial commit: add worker, scanner, and task files)
require('dotenv').config();
const Queue = require('bull');
const mongoose = require('mongoose');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
<<<<<<< HEAD
const os = require('os');

const REDIS_URL = process.env.REDIS_URL || 'redis://127.0.0.1:6379';
const SCAN_SAVE_ROOT = process.env.SCAN_SAVE_ROOT || '/var/lib/vap/scans';
const PY_SCRIPT = process.env.PY_SCRIPT_PATH || '/opt/python/poc_runner_sqlmap.py';
const MAX_RUNTIME = parseInt(process.env.WORKER_MAX_RUNTIME || '900'); // seconds

// connect db
mongoose.connect(process.env.MONGO_URI || 'mongodb://127.0.0.1:27017/vap', { useNewUrlParser:true, useUnifiedTopology:true });

// minimal Job model (adjust to your schema)
const JobSchema = new mongoose.Schema({ request_id:String, payload:Object, status:String, created_at:Date, updated_at:Date, report_id:String, attempts:Number });
const Job = mongoose.model('Job', JobSchema);

// Report model
const ReportSchema = new mongoose.Schema({ jobDbId:String, target:Object, findings:Array, artifacts:Object, status:String, created_at:Date, completed_at:Date });
const Report = mongoose.model('Report', ReportSchema);

// queue
const scanQueue = new Queue('scanQueue', REDIS_URL);

// helpers
function makeSaveDir(jobId){
  const d = path.join(SCAN_SAVE_ROOT, jobId);
  fs.mkdirSync(d, {recursive:true, mode:0o750});
  return d;
}

function isAllowedTarget(targetUrl, allowed){
  // very simple check: ensure hostname is in allowed_subdomains if provided
  try{
    const u = new URL(targetUrl);
    if(!allowed || allowed.length === 0) return true;
    return allowed.some(s => u.hostname === s || u.hostname.endsWith('.' + s));
  } catch(e){
    return false;
  }
}

async function runPython(payloadPath, outdir){
  return new Promise((resolve, reject)=>{
    const py = spawn('python3', [PY_SCRIPT, '--payload', payloadPath, '--outdir', outdir], {
      stdio: ['ignore','pipe','pipe'],
      detached: false
    });

    let stdout = '', stderr = '';
    py.stdout.on('data', d=> stdout += d.toString());
    py.stderr.on('data', d=> stderr += d.toString());

    const timeout = setTimeout(()=>{
      try { py.kill('SIGKILL'); } catch(e){}
      reject(new Error('python runner timeout'));
    }, MAX_RUNTIME*1000);

    py.on('close', code=>{
      clearTimeout(timeout);
      resolve({ code, stdout, stderr });
    });
    py.on('error', err=>{
      clearTimeout(timeout);
      reject(err);
    });
  });
}

// worker processor
scanQueue.process(parseInt(process.env.WORKER_CONCURRENCY || '1'), async (job) => {
  const jobDbId = job.data.jobDbId;
  const jobDoc = await Job.findById(jobDbId);
  if(!jobDoc) throw new Error('job missing');

  jobDoc.status = 'running';
  jobDoc.updated_at = new Date();
  jobDoc.attempts = (jobDoc.attempts||0) + 1;
  await jobDoc.save();

  const saveDir = makeSaveDir(jobDbId);
  const payloadFile = path.join(saveDir, 'payload.json');
  fs.writeFileSync(payloadFile, JSON.stringify(jobDoc.payload, null, 2), {mode:0o640});

  // validate target vs allowed_subdomains
  const targetUrl = jobDoc.payload.target && jobDoc.payload.target.url;
  const allowed = jobDoc.payload.target && jobDoc.payload.target.allowed_subdomains;
  if(!isAllowedTarget(targetUrl, allowed)){
    jobDoc.status='failed';
    await jobDoc.save();
    throw new Error('target not allowed by allowed_subdomains policy');
  }

  // run python runner (inside container is recommended)
  try{
    const res = await runPython(payloadFile, saveDir);
    fs.writeFileSync(path.join(saveDir,'runner_stdout.log'), res.stdout);
    fs.writeFileSync(path.join(saveDir,'runner_stderr.log'), res.stderr);
    if(res.code !== 0){
      jobDoc.status='failed';
      await jobDoc.save();
      throw new Error('python runner returned non-zero: ' + res.code);
    }

    // load summary if exists
    const summaryPath = path.join(saveDir,'summary.json');
    let summary = { error:'no summary found' };
    if(fs.existsSync(summaryPath)){
      summary = JSON.parse(fs.readFileSync(summaryPath,'utf8'));
    }

    // save report doc
    const reportDoc = new Report({ jobDbId, target: jobDoc.payload.target, findings:[jobDoc.payload.finding], artifacts:{ saveDir, summaryPath }, status:'scanned', created_at:new Date(), completed_at:new Date() });
    reportDoc.findings[0].evidence = summary;
    await reportDoc.save();

    jobDoc.status='completed';
    jobDoc.report_id = reportDoc._id;
    jobDoc.updated_at = new Date();
    await jobDoc.save();
    console.log('job completed', jobDbId);
    return Promise.resolve();
  } catch(err){
    // mark failed, maybe requeue or escalate
    jobDoc.status='failed';
    jobDoc.updated_at = new Date();
    await jobDoc.save();
    console.error('job error', jobDbId, err.message || err);
    throw err;
  }
});

=======

// إعداد الاتصال بـ MongoDB
mongoose.connect(process.env.MONGO_URI)
  .then(() => console.log('[MongoDB] ✅ Connected'))
  .catch(err => console.error('[MongoDB] ❌ Connection error:', err));

const REDIS_URL = process.env.REDIS_URL || 'redis://127.0.0.1:6379';
const jobQueue = new Queue('scanner', REDIS_URL);

jobQueue.process(async (job, done) => {
  console.log(`\n⚙️  Running job ${job.id}`);
  console.log('--------------------------------');

  try {
    const jobData = job.data;
    const jobFile = path.join(__dirname, `payload-${job.id}.json`);
    const outputDir = path.join(__dirname, `output-${job.id}`);

    fs.writeFileSync(jobFile, JSON.stringify(jobData, null, 2));
    fs.mkdirSync(outputDir, { recursive: true });

    console.log(`[Worker] Launching Python scanner...`);

    const pythonProcess = spawn('python3', [
      'poc_runner_scanner.py',
      '--payload', jobFile,
      '--outdir', outputDir
    ]);

    pythonProcess.stdout.on('data', (data) => process.stdout.write(`[PYTHON] ${data}`));
    pythonProcess.stderr.on('data', (data) => process.stderr.write(`[PYTHON_ERR] ${data}`));

    pythonProcess.on('close', (code) => {
      console.log(`\n[Worker] Python exited with code ${code}`);
      if (code === 0) {
        console.log(`[Worker] ✅ Job ${job.id} done successfully.`);
        done();
      } else {
        done(new Error(`Python process failed with code ${code}`));
      }
    });
  } catch (err) {
    console.error('[Worker] Job error:', err);
    done(err);
  }
});

console.log('\n🚀 Worker started and waiting for jobs...');
>>>>>>> a8b0cf8 (Initial commit: add worker, scanner, and task files)
