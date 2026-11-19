// Local-only design preview. All APIs use synthetic fixtures, never a provider.
const {workstation} = require("./fixtures/workstation.cjs");
const cleanup=[];
async function main() {
  const fixture=await workstation({after:fn=>cleanup.push(fn)},false,{
    isolated:true,historySize:12,
    jobs:[
      {id:"hermes:preview",provider:"hermes",title:"測試資料 / 設計檢查",status:"idle"},
      {id:"claude_code:preview",provider:"claude_code",title:"測試資料 / 等待確認",status:"waiting_approval"},
      {id:"codex:preview",provider:"codex",title:"設計預覽 / 非正式工作",status:"running"},
      {id:"gemini:preview",provider:"gemini",title:"測試資料 / 文件整理",status:"completed"}
    ],
    handle:async(req,res,url)=>{
      if(url.pathname!=="/preview") return false;
      res.writeHead(200,{"Content-Type":"text/html; charset=utf-8","Cache-Control":"no-store"});
      res.end('<!doctype html><title>Pocket local fixture</title><script>localStorage.setItem("pocket.console.token","synthetic-owner");location.replace("/console#session=codex%3Apreview")</script>');
      return true;
    }
  });
  await fixture.context.close();
  console.log("Fixture preview (loopback only): "+fixture.origin+"/preview");
}
for(const signal of ["SIGINT","SIGTERM"]) process.once(signal,async()=>{
  for(const close of cleanup.reverse()) await close();
  process.exit(0);
});
main().catch(error=>{console.error(error);process.exit(1);});
