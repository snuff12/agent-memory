// Actual qmd SDK, isolated state, native default models. No questions in index.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import readline from 'node:readline';

const workspace = path.resolve(process.argv[2]);
const serve = process.argv.includes('--serve');
const noBuild = process.argv.includes('--no-build');
const mode = process.argv.find(arg=>arg.startsWith('--mode='))?.split('=')[1] ?? 'full';
if (!['full','lexical','vector','hybrid'].includes(mode)) throw new Error('Invalid qmd comparison mode');
process.env.XDG_CACHE_HOME = path.join(workspace, 'runs/qmd-cache');
process.env.HF_HUB_DISABLE_TELEMETRY = '1';
const { createStore, extractSnippet } = await import(pathToFileURL(path.join(workspace, 'upstream/qmd/dist/index.js')));
const corpus = JSON.parse(fs.readFileSync(path.join(workspace, 'corpus.json'), 'utf8'));
const sourceRoot = path.join(workspace, 'memory/knowledge');
const store = await createStore({dbPath:path.join(workspace,'runs/qmd.sqlite'),
  config:{collections:{comparison:{path:sourceRoot,pattern:'**/*'}}}});
let start = performance.now();
if (!noBuild) {
  await store.update();
  await store.embed();
}
if (!noBuild) fs.writeFileSync(path.join(workspace, 'runs/qmd-build.json'), JSON.stringify({build_ms:performance.now()-start}));
else fs.appendFileSync(path.join(workspace,'runs/qmd-reloads.jsonl'),JSON.stringify({reload_ms:performance.now()-start})+'\n');

async function retrieve(query) {
  const results = mode==='lexical' ? await store.searchLex(query,{limit:20})
    : mode==='vector' ? await store.searchVector(query,{limit:20})
    : mode==='hybrid' ? await store.search({queries:[{type:'lex',query},{type:'vec',query}],rerank:false,limit:20})
    : await store.search({query,limit:20});
  const output = [];
  for (const result of results) {
    const relative = (result.file ?? result.filepath).replace(/^qmd:\/\/comparison\//, '');
    const file = path.join(sourceRoot, relative);
    if (!fs.existsSync(file)) continue;
    const body = fs.readFileSync(file,'utf8');
    const navigation=extractSnippet(body,query,500,result.chunkPos);
    const lineStart=body.split('\n').slice(0,navigation.line-1).join('\n').length;
    const left = result.bestChunkPos ?? result.chunkPos ?? lineStart;
    const right = left + (result.bestChunk?.length ?? 500);
    let mapped = corpus.filter(row=>row.path===relative).map(row=>{
      const offset=body.indexOf(row.text);
      return {row,offset,overlap:offset<0?0:Math.max(0,Math.min(offset+row.text.length,right)-Math.max(offset,left))};
    }).filter(item=>item.overlap>0).sort((a,b)=>b.overlap-a.overlap).slice(0,2);
    if (!mapped.length) mapped=corpus.filter(row=>row.path===relative).slice(0,1).map(row=>({row}));
    for (const {row} of mapped) output.push({source:row.id,score:result.score,excerpt:(result.bestChunk??navigation.snippet).slice(0,240)});
  }
  return output;
}

try {
  if (serve) {
    process.stdout.write(JSON.stringify({ready:true})+'\n');
    for await (const line of readline.createInterface({input:process.stdin})) {
      const request=JSON.parse(line);start=performance.now();
      const results=await retrieve(request.query);
      process.stdout.write(JSON.stringify({results,retrieval_ms:performance.now()-start})+'\n');
    }
  } else {
    const queries=JSON.parse(fs.readFileSync(path.join(workspace,'queries.json'),'utf8'));
    for (const request of queries) {
      start=performance.now();const results=await retrieve(request.query);
      const filename=mode==='full'?'qmd-raw.jsonl':`qmd-${mode}-raw.jsonl`;
      fs.appendFileSync(path.join(workspace,'runs',filename),JSON.stringify({id:request.id,results,retrieval_ms:performance.now()-start})+'\n');
      process.stderr.write('qmd retrieved '+request.id+'\n');
    }
  }
} finally {
  await store.close();
}
