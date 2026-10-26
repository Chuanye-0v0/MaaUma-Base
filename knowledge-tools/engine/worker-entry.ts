import {dispatch} from './adapter';
self.addEventListener('message',event=>{self.postMessage({id:event.data?.id,result:dispatch(event.data?.request||event.data)});});
