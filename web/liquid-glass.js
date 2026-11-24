(() => {
  'use strict';

  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const NS = 'http://www.w3.org/2000/svg';
  const controls = new WeakMap();
  let filterCount=0;

  function make(tag, name) {
    const node = document.createElement(tag);
    node.className = name;
    return node;
  }

  function svg(tag, values) {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(values)) node.setAttribute(key, value);
    return node;
  }

  function rimImage(canvas, context, width, height) {
    canvas.width = width;
    canvas.height = height;
    const image = context.createImageData(width, height);
    const cx = width/2, cy = height/2;
    const radius = Math.min(13,cx,cy);
    const rim = Math.min(17,cx*.8,cy*.8);
    for (let y=0,offset=0; y<height; y++) {
      for (let x=0; x<width; x++,offset+=4) {
        const dx=x+.5-cx, dy=y+.5-cy;
        const qx=Math.abs(dx)-(cx-radius), qy=Math.abs(dy)-(cy-radius);
        const rx=Math.max(qx,0), ry=Math.max(qy,0);
        const length=Math.hypot(rx,ry);
        const distance=radius-length-Math.min(0,Math.max(qx,qy));
        const t=Math.max(0,Math.min(1,1-distance/rim));
        const bend=108*t*t*(3-2*t);
        const nx=length?rx/length:Number(qx>qy);
        const ny=length?ry/length:Number(qy>=qx);
        image.data[offset]=128-bend*nx*Math.sign(dx);
        image.data[offset+1]=128-bend*ny*Math.sign(dy);
        image.data[offset+2]=128;
        image.data[offset+3]=255;
      }
    }
    context.putImageData(image,0,0);
    return canvas.toDataURL();
  }

  function optics() {
    const definition = svg('svg', {'aria-hidden': 'true', width: 0, height: 0});
    definition.classList.add('glass-optics-defs');
    const id='guanlan-nav-refraction-'+(++filterCount);
    const filter = svg('filter', {id, x:'-12%', y:'-20%', width:'124%',
      height:'140%', primitiveUnits:'userSpaceOnUse', 'color-interpolation-filters':'sRGB'});
    const flat = svg('feFlood', {'flood-color':'#808080',result:'flat'});
    const rim = svg('feImage', {preserveAspectRatio:'none',result:'rim'});
    const field = svg('feMerge', {result:'field'});
    field.append(svg('feMergeNode',{in:'flat'}),svg('feMergeNode',{in:'rim'}));
    // The neutral 8-bit value (128) must map to exactly 0.5 for zero displacement.
    const centered = svg('feComponentTransfer',{in:'field',result:'centered'});
    const normalize = {type:'linear',slope:1,intercept:.5-128/255};
    centered.append(svg('feFuncR',normalize),svg('feFuncG',normalize));
    const refraction = svg('feDisplacementMap', {in:'SourceGraphic',in2:'centered',scale:0,
      xChannelSelector: 'R', yChannelSelector: 'G'});
    filter.append(flat,rim,field,centered,refraction);
    definition.append(filter);
    document.body.append(definition);
    return {id,rim,refraction};
  }

  function createNav(nav) {
    const lens = make('div', 'glass-nav-lens');
    lens.setAttribute('aria-hidden', 'true');
    lens.inert = true;
    const sample = make('div', 'glass-nav-sample');
    const buttons = [...nav.querySelectorAll('[data-main-page],[data-glass-value]')];
    const copies = buttons.map(button => {
      const copy = button.cloneNode(true);
      copy.className = 'glass-nav-copy';
      for (const attribute of ['data-main-page','data-glass-value','title','aria-current','aria-pressed']) copy.removeAttribute(attribute);
      copy.tabIndex = -1;
      sample.append(copy);
      return copy;
    });
    lens.append(sample);
    nav.append(lens);
    const {id:filterId,rim,refraction} = optics();
    const canvas = document.createElement('canvas');
    const context = canvas.getContext('2d');
    let position, selected, target, animation;

    function draw(box, ripple = 0) {
      position = box;
      lens.style.transform = `translate(${box.x}px,${box.y}px)`;
      lens.style.width = `${box.width}px`;
      lens.style.height = `${box.height}px`;
      sample.style.transform = `translate(${-box.x}px,${-box.y}px)`;
      // Follow the travelling lens in the stationary text layer's coordinates.
      sample.style.filter = context&&ripple ? `url(#${filterId})` : 'none';
      if (context&&ripple) {
        for (const [key,value] of Object.entries(box)) rim.setAttribute(key,value.toFixed(2));
        refraction.setAttribute('scale',ripple.toFixed(2));
      }
    }

    function measure() {
      const bounds = nav.getBoundingClientRect();
      return buttons.map(button => {
        const rect = button.getBoundingClientRect();
        return {x:rect.left-bounds.left-nav.clientLeft,y:rect.top-bounds.top-nav.clientTop,
          width:rect.width,height:rect.height};
      });
    }

    function layout(id, animate) {
      if (animate && id === selected) return;
      const index = buttons.findIndex(button => (button.dataset.mainPage||button.dataset.glassValue) === id);
      if (index < 0) {
        cancelAnimationFrame(animation);
        nav.classList.remove('glass-nav-ready', 'glass-nav-moving');
        selected = target = position = undefined;
        return;
      }
      const boxes = measure();
      const next = boxes[index];
      if (!next.width || !next.height) return;
      if (id === selected && target && Object.keys(next).every(key => Math.abs(next[key]-target[key]) < .25)) return;
      sample.style.width = `${nav.clientWidth}px`;
      sample.style.height = `${nav.clientHeight}px`;
      const width=Math.round(next.width), height=Math.round(next.height);
      if (context&&(canvas.width!==width||canvas.height!==height||!rim.hasAttribute('href'))) {
        rim.setAttribute('href',rimImage(canvas,context,width,height));
      }
      copies.forEach((copy, order) => {
        const {x,y,width,height} = boxes[order];
        Object.assign(copy.style,{left:`${x}px`,top:`${y}px`,width:`${width}px`,height:`${height}px`});
      });
      const previous = selected;
      selected = id;
      target = next;
      const start = position;
      cancelAnimationFrame(animation);
      nav.classList.add('glass-nav-ready');
      nav.classList.remove('glass-nav-moving');
      if (!animate || !start || previous === id || reducedMotion.matches) {
        draw(next);
        return;
      }
      const dx = next.x-start.x, dy = next.y-start.y;
      const distance = Math.hypot(dx,dy);
      const duration = 490+Math.min(180,distance*.56);
      const started = performance.now();
      nav.classList.add('glass-nav-moving');
      function frame(now) {
        const time = Math.min(1,(now-started)/duration);
        if (time === 1) {
          draw(next);
          nav.classList.remove('glass-nav-moving');
          return;
        }
        const progress = time*time*(3-2*time);
        const wave = Math.sin(time*Math.PI);
        const stretch = wave*Math.min(13,distance*.11);
        const horizontal = Math.abs(dx)>Math.abs(dy);
        draw({
          x:start.x+dx*progress-(horizontal?stretch/2:0),
          y:start.y+dy*progress-(horizontal?0:stretch/2),
          width:start.width+(next.width-start.width)*progress+(horizontal?stretch:0),
          height:start.height+(next.height-start.height)*progress+(horizontal?0:stretch),
        },wave*22);
        animation=requestAnimationFrame(frame);
      }
      animation=requestAnimationFrame(frame);
    }

    new ResizeObserver(() => { if (selected) layout(selected,false); }).observe(nav);
    reducedMotion.addEventListener('change', () => {
      if (!target) return;
      cancelAnimationFrame(animation);
      nav.classList.remove('glass-nav-moving');
      draw(target);
    });
    return {layout};
  }

  window.GuanlanGlass = {
    select(nav, id) {
      if (!controls.has(nav)) controls.set(nav,createNav(nav));
      controls.get(nav).layout(id,true);
    },
  };

})();
