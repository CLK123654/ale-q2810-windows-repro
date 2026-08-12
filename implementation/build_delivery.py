from __future__ import annotations
import argparse,atexit,csv,json,shutil,subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parent
REQUIRED={'README.md','content_catalog.csv','base_events.csv','late_events.csv','release_plan.csv','release_policy.json','starter/blocking_refresh.sql'}
def run(cmd,stdin=None):
 r=subprocess.run(cmd,input=stdin,text=True,encoding='utf-8',errors='replace',capture_output=True,timeout=300)
 if r.returncode:raise RuntimeError(r.stdout+r.stderr)
 return r.stdout
def psql(binary,url):return [binary,'--dbname',url,'-X','--set','ON_ERROR_STOP=1','--quiet']
def rows(path):
 with path.open(encoding='utf-8-sig',newline='') as h:return list(csv.DictReader(h))
def lit(value):return "'"+str(value).replace("'","''")+"'"
def insert_values(binary,url,table,data,fields,columns):
 values=['('+','.join(lit(row[field]) for field in fields)+')' for row in data]
 run(psql(binary,url)+['--command',f"INSERT INTO rank_release.{table}({','.join(columns)}) VALUES"+','.join(values)])
def export(binary,url,query,path):
 path.write_text(run(psql(binary,url)+['--command',f'COPY ({query}) TO STDOUT WITH(FORMAT CSV,HEADER TRUE)']),encoding='utf-8',newline='')
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--input',required=True);ap.add_argument('--output',required=True);ap.add_argument('--psql',required=True);ap.add_argument('--database-url',required=True);a=ap.parse_args();source=Path(a.input).resolve();output=Path(a.output).resolve()
 if output.exists():shutil.rmtree(output)
 finished={'ok':False}
 def cleanup():
  if not finished['ok'] and output.exists():shutil.rmtree(output)
 atexit.register(cleanup)
 present={p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
 if present!=REQUIRED:raise ValueError('发布材料集合发生变化')
 catalog=rows(source/'content_catalog.csv');base=rows(source/'base_events.csv');late=rows(source/'late_events.csv');plan=rows(source/'release_plan.csv');policy=json.loads((source/'release_policy.json').read_text(encoding='utf-8'))
 fault_sql=(source/'starter/blocking_refresh.sql').read_text(encoding='utf-8')
 watermark_at=fault_sql.find('UPDATE analytics.refresh_watermark')
 refresh_at=fault_sql.find('REFRESH MATERIALIZED VIEW')
 if watermark_at<0 or refresh_at<0 or watermark_at>refresh_at:raise ValueError('故障SQL与变更说明不一致')
 if len({x['content_id'] for x in catalog})!=len(catalog):raise ValueError('content_id重复')
 events=base+late
 if len({x['event_id'] for x in events})!=len(events):raise ValueError('event_id重复')
 if [x['stage'] for x in plan]!=policy['rollout_order']:raise ValueError('批次顺序与策略不一致')
 if set(policy['event_types'])!={'impression','click'}:raise ValueError('事件类型策略不完整')
 output.mkdir(parents=True);(output/'sql').mkdir();(output/'results').mkdir();shutil.copy2(ROOT/'solution.sql',output/'sql/solution.sql')
 run(psql(a.psql,a.database_url),"DROP SCHEMA IF EXISTS rank_release CASCADE;\n"+(ROOT/'solution.sql').read_text(encoding='utf-8'))
 insert_values(a.psql,a.database_url,'content_catalog',catalog,['content_id','title','active'],['content_id','title','active'])
 event_fields=['event_id','occurred_at_utc','received_at_utc','region','content_id','event_type','dwell_ms'];event_cols=['event_id','occurred_at','received_at','region','content_id','event_type','dwell_ms']
 insert_values(a.psql,a.database_url,'content_event',base,event_fields,event_cols)
 release_rows=[]
 for index,item in enumerate(plan):
  if index==1:insert_values(a.psql,a.database_url,'content_event',late,event_fields,event_cols)
  run(psql(a.psql,a.database_url)+['--command',f"SELECT rank_release.publish_batch({lit(item['stage'])},{lit(item['batch_id'])},{lit(item['cutoff_received_at'])},{int(policy['change_window']['lock_wait_ms'])})"])
  release_rows.append(item)
 export(a.psql,a.database_url,'SELECT batch_id,stage,cutoff_received_at,source_event_count,published_row_count,status FROM rank_release.release_batch ORDER BY created_at',output/'results/release_batches.csv')
 export(a.psql,a.database_url,'SELECT batch_id,hour_start,region,content_id,impression_count,click_count,total_dwell_ms,ctr_pct,region_rank FROM rank_release.published_rank ORDER BY batch_id,hour_start,region,region_rank,content_id',output/'results/published_ranks.csv')
 export(a.psql,a.database_url,'SELECT a.batch_id,b.stage,b.cutoff_received_at,b.source_event_count,b.published_row_count FROM rank_release.active_release a JOIN rank_release.release_batch b USING(batch_id)',output/'results/active_release.csv')
 base_id=plan[0]['batch_id'];late_id=plan[1]['batch_id']
 export(a.psql,a.database_url,f"SELECT n.hour_start,n.region,n.content_id,o.region_rank old_rank,n.region_rank new_rank,o.impression_count old_impressions,n.impression_count new_impressions,o.click_count old_clicks,n.click_count new_clicks,o.total_dwell_ms old_total_dwell_ms,n.total_dwell_ms new_total_dwell_ms,o.ctr_pct old_ctr_pct,n.ctr_pct new_ctr_pct FROM rank_release.published_rank o JOIN rank_release.published_rank n USING(hour_start,region,content_id) WHERE o.batch_id={lit(base_id)} AND n.batch_id={lit(late_id)} AND ROW(o.region_rank,o.impression_count,o.click_count,o.total_dwell_ms,o.ctr_pct) IS DISTINCT FROM ROW(n.region_rank,n.impression_count,n.click_count,n.total_dwell_ms,n.ctr_pct) ORDER BY n.hour_start,n.region,n.content_id",output/'results/rank_changes.csv')
 (output/'RELEASE-NOTES.md').write_text(f"内容榜发布安排在{policy['change_window']['start']}至{policy['change_window']['end']}。影响范围为{policy['change_window']['impact']}，数据库锁等待预算为{policy['change_window']['lock_wait_ms']}毫秒。先保留baseline批次，再切换late_update批次；切换后观察{policy['change_window']['observation_minutes']}分钟，核对{('、'.join(policy['observation_fields']))}。结果异常时{policy['rollback']}。旧脚本会在刷新前推进水位，本次发布不再沿用这个顺序。\n",encoding='utf-8')
 (output/'README.md').write_text('sql目录保存发布表与事务函数。results中的release_batches.csv记录批次边界，published_ranks.csv保存不可变榜单，active_release.csv指出运营台当前读取批次，rank_changes.csv供运营核对迟到事件带来的变化。\n',encoding='utf-8')
 finished['ok']=True
if __name__=='__main__':main()
