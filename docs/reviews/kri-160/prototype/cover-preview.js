/* Only animate visible covers. No external media or services. */
(()=>{
  const seen=new WeakSet(),covers=new Set(),videoTicks=new WeakMap(),reduce=matchMedia('(prefers-reduced-motion: reduce)');
  function watchVideo(art){
    clearTimeout(videoTicks.get(art));
    const video=art.querySelector('video');if(!video)return;
    if(!art.classList.contains('is-previewing')){video.pause();return}
    const animation=art.querySelector('.cover-track').getAnimations().find(a=>a.animationName==='formatCoverSwipe');
    const phase=Number(animation?.currentTime||0)%10000,inFrame=phase>=2600&&phase<6500;
    if(inFrame){if(art.dataset.inVideoFrame!=='true')video.currentTime=0;if(video.paused)video.play().catch(()=>{})}else video.pause();
    art.dataset.inVideoFrame=String(inFrame);
    videoTicks.set(art,setTimeout(()=>watchVideo(art),100));
  }
  function sync(art){
    const motion=!reduce.matches&&!document.hidden&&art.dataset.visible==='true'&&art.dataset.paused!=='true'&&!art.closest('.journey-board');
    art.classList.toggle('is-previewing',motion);
    const toggle=art.closest('.post-choice')?.querySelector('.cover-toggle');
    if(toggle){toggle.hidden=reduce.matches;toggle.setAttribute('aria-pressed',String(art.dataset.paused==='true'));toggle.setAttribute('aria-label',art.dataset.paused==='true'?'Play format preview':'Pause format preview');toggle.querySelector('path').setAttribute('d',art.dataset.paused==='true'?'m9 5 10 7-10 7Z':'M8 6v12M16 6v12')}
    const video=art.querySelector('video');
    if(video){video.muted=true;watchVideo(art)}
  }
  const visible=new IntersectionObserver(entries=>{for(const entry of entries){entry.target.dataset.visible=String(entry.isIntersecting&&entry.intersectionRatio>=.65);sync(entry.target)}},{threshold:[0,.65,1]});
  function scan(){for(const art of covers){if(!art.isConnected){visible.unobserve(art);clearTimeout(videoTicks.get(art));art.querySelector('video')?.pause();covers.delete(art)}}document.querySelectorAll('.swipe-art').forEach(art=>{if(seen.has(art))return;seen.add(art);covers.add(art);sync(art);visible.observe(art)})}
  document.addEventListener('click',e=>{const toggle=e.target.closest('[data-do="cover:toggle"]');if(!toggle)return;e.preventDefault();e.stopImmediatePropagation();const art=toggle.closest('.post-choice').querySelector('.swipe-art');art.dataset.paused=art.dataset.paused==='true'?'false':'true';sync(art)},true);
  document.addEventListener('visibilitychange',()=>covers.forEach(sync));reduce.addEventListener('change',()=>covers.forEach(sync));
  new MutationObserver(scan).observe(document.getElementById('root'),{childList:true,subtree:true});scan();
  if(new URLSearchParams(location.search).get('focus')==='post')document.querySelector('.format-track')?.scrollTo({left:9999});
})();
