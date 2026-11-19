'use strict';
(() => {
  const $=id=>document.getElementById(id);
  let session=null, mode='login', busy=false;
  const wantsAdmin=new URLSearchParams(location.search).get('next')==='/admin/';
  function message(note='',error=false){$('status').textContent=note;$('status').dataset.error=String(error);}
  function setBusy(value){
    busy=value;
    for(const id of ['submit','logout','login-tab','register-tab','retry'])$(id).disabled=value;
  }
  function changeMode(next){
    mode=next;
    const register=mode==='register';
    $('login-tab').setAttribute('aria-pressed',String(!register));
    $('register-tab').setAttribute('aria-pressed',String(register));
    $('credentials').setAttribute('action','/api/account/'+mode);
    $('password').setAttribute('autocomplete',register?'new-password':'current-password');
    $('password').value='';
    $('submit').textContent=register?'注册账户':'登录';
    $('register-hint').hidden=!register;
    message();
  }
  function paint(next){
    if(!next || typeof next.csrf_token!=='string' || !next.csrf_token)throw Error('账户服务响应异常，请重新连接。');
    session=next;
    const user=next.user,admin=user?.role==='admin';
    $('guest').hidden=Boolean(user);$('member').hidden=!user;$('retry').hidden=true;
    $('name').textContent=user?.username||'';
    $('role').textContent=user?(admin?'管理员':'普通账户'):'';
    $('admin-link').hidden=!admin;
    $('intro').textContent=user?'已登录观澜。':'查看公开行情和资讯无需登录。';
    if(admin && wantsAdmin)location.replace('/admin/');
  }
  async function call(endpoint,body){
    const options={credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(12000)};
    if(body!==undefined){
      options.method='POST';
      options.headers={'Content-Type':'application/json','X-CSRF-Token':session.csrf_token};
      options.body=JSON.stringify(body);
    }
    const response=await fetch('/api/account/'+endpoint,options);
    let data;
    try{data=await response.json();}catch{throw Error('账户服务暂时无响应，请重试。');}
    if(!response.ok){
      const error=Error(typeof data.message==='string'?data.message:'账户操作未完成，请重试。');
      error.status=response.status;throw error;
    }
    return data;
  }
  function failed(error){
    message(error.name==='TimeoutError'||error instanceof TypeError?'暂时无法连接账户服务，请重试。':error.message,true);
  }
  async function refresh(){
    if(busy)return;
    setBusy(true);
    try{paint(await call('session'));message();}
    catch(error){session=null;$('guest').hidden=true;$('member').hidden=true;$('retry').hidden=false;failed(error);}
    finally{setBusy(false);}
  }
  async function submit(endpoint,body){
    if(busy||!session)return;
    setBusy(true);message();
    try{const next=await call(endpoint,body);$('password').value='';paint(next);message(endpoint==='logout'?'已退出登录。':'');}
    catch(error){
      if([401,403].includes(error.status)){
        try{paint(await call('session'));}catch{$('retry').hidden=false;}
      }
      failed(error);
    }finally{setBusy(false);}
  }
  $('login-tab').addEventListener('click',()=>changeMode('login'));
  $('register-tab').addEventListener('click',()=>changeMode('register'));
  $('credentials').addEventListener('submit',e=>{
    e.preventDefault();submit(mode,{username:$('username').value.trim(),password:$('password').value});
  });
  $('logout').addEventListener('click',()=>submit('logout',{}));
  $('retry').addEventListener('click',refresh);
  $('register-hint').textContent='用户名为 3–32 位，可用英文字母、数字、下划线、点和短横线，首位为字母或数字。密码为 12–128 个字符。';
  refresh();
})();
