/* Encode station samples away from the dashboard/Board rendering thread. */
'use strict';
let canvas, context;
self.onmessage = async ({data:{id,bitmap}}) => {
  try {
    canvas ||= new OffscreenCanvas(bitmap.width,bitmap.height);
    if (canvas.width!==bitmap.width) canvas.width=bitmap.width;
    if (canvas.height!==bitmap.height) canvas.height=bitmap.height;
    context ||= canvas.getContext('2d');
    context.clearRect(0,0,canvas.width,canvas.height);
    context.drawImage(bitmap,0,0);
    let blob;
    for (const quality of [.82,.70,.55]) {
      blob=await canvas.convertToBlob({type:'image/jpeg',quality});
      if (blob.size<=1048576) break;
    }
    // Return the exact captured picture with its JPEG. The dashboard may already
    // be preparing a newer sample when this frame's server response arrives.
    self.postMessage({id,blob,bitmap},[bitmap]);
  } catch (error) {
    bitmap.close(); self.postMessage({id,error:error.message || '图像编码失败'});
  }
};
