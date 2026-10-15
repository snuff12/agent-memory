// Decode the native binary with upstream's generated protobuf implementation.
// Generated code and decoded source indexes stay outside this checkout.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';

const workspace=path.resolve(process.argv[2]);
const input=path.resolve(process.argv[3] ?? path.join(workspace,'runs/index.scip'));
const output=path.resolve(process.argv[4] ?? path.join(workspace,'runs/scip-index.json'));
if (fs.existsSync(output)) throw new Error('Refusing to overwrite an existing decoded index');
const require=createRequire(import.meta.url);
const ts=require(path.join(workspace,'upstream/qmd/node_modules/typescript'));
const upstream=path.join(workspace,'upstream/scip-python/packages/pyright-scip/src/scip.ts');
const code=ts.transpileModule(fs.readFileSync(upstream,'utf8'),{
  compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}
}).outputText;
const compiled=path.join(workspace,'envs/scip/native-scip-decoder.cjs');
fs.writeFileSync(compiled,code);
const {scip}=require(compiled);
const index=scip.Index.deserializeBinary(fs.readFileSync(input)).toObject();
fs.writeFileSync(output,JSON.stringify(index));
process.stdout.write(JSON.stringify({documents:index.documents.length})+'\n');
