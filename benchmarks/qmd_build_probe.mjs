// Fresh native index with existing model files; never replaces the scored DB.
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const workspace=path.resolve(process.argv[2]);
process.env.XDG_CACHE_HOME=path.join(workspace,'runs/qmd-cache');
const dbPath=path.join(workspace,'runs/qmd-warm-cache-build-probe.sqlite');
if (fs.existsSync(dbPath)) throw new Error('Refusing to overwrite an existing build probe');
const {createStore}=await import(pathToFileURL(path.join(workspace,'upstream/qmd/dist/index.js')));
const started=performance.now();
const store=await createStore({dbPath,config:{collections:{comparison:{path:path.join(workspace,'memory/knowledge'),pattern:'**/*'}}}});
try {
  const scanStart=performance.now();await store.update();const scanMs=performance.now()-scanStart;
  const embedStart=performance.now();await store.embed();const embedMs=performance.now()-embedStart;
  const report={scope:'Fresh DB; previously downloaded model files and unpurged OS cache; concurrent diagnostic jobs; no query/model generation/reranking',
    scan_ms:scanMs,embedding_and_model_load_ms:embedMs,total_ms:performance.now()-started,status:await store.getStatus()};
  fs.writeFileSync(path.join(workspace,'runs/qmd-warm-cache-build-probe.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify({scan_ms:scanMs,embedding_and_model_load_ms:embedMs,total_ms:report.total_ms}));
} finally {await store.close();}
