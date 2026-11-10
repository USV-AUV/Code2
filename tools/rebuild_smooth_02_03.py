#!/usr/bin/env python3
"""Re-render smooth, fixed-canvas GIFs 02 & 03 from the provided 3AUV replay.

Source: source_data/TMC_2AUV_3AUV_Three_Matrices_Real_Data.zip.

Data integrity conventions:
  - The JSON contains all 1,201 native samples at one-second spacing.
  - Link quality and report age are linearly interpolated *between adjacent
    native samples* for GIF display only. Every integer-second frame is exact.
  - Local and USV-confirmed parent vectors are discrete, never interpolated:
    they are the installed/confirmed state at floor(display_time).
  - Self links and missing report ages are shown at Z=0 in the 3D plot to
    match the supplied Fig. 3 visualization convention; they are NOT fresh
    link observations. The matrix beside the surface displays them as '--'.
  - Spatial interpolation is for surface/heatmap rendering only.
  - Fixed figure canvas, add_axes coordinates, view angle, and axes limits
    prevent layout-induced horizontal or vertical drift.

Usage:
  python tools/rebuild_smooth_02_03.py
"""
from __future__ import annotations
import io
import json
from pathlib import Path
import zipfile
import numpy as np
from PIL import Image
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import cm, colors, font_manager
from scipy.ndimage import gaussian_filter, zoom
from scipy.interpolate import PchipInterpolator
from mpl_toolkits.mplot3d import proj3d

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'assets'
SRC = ROOT / 'source_data' / 'TMC_2AUV_3AUV_Three_Matrices_Real_Data.zip'

_fts = set(f.name for f in font_manager.fontManager.ttflist)
_SERIF = next((f for f in ['Times New Roman','Tinos','Liberation Serif','DejaVu Serif'] if f in _fts), 'DejaVu Serif')
plt.rcParams.update({
    'font.family': _SERIF,
    'mathtext.fontset': 'stix',
    'pdf.fonttype': 42,
    'ps.fonttype': 42,
    'axes.unicode_minus': False,
    'figure.facecolor': 'white',
    'savefig.facecolor': 'white',
})

with zipfile.ZipFile(SRC) as z:
    OBJ = json.loads(z.read('3auv_full_matrix_series.json'))
SERIES = OBJ['series']
NAMES = OBJ['metadata']['node_order']
assert len(SERIES) == 1201 and [d['time_s'] for d in SERIES] == list(range(1201))
N = len(NAMES)

QUALITY = np.asarray([d['link_quality'] for d in SERIES], dtype=np.float64)
AGE_RAW = np.full((len(SERIES),N,N),np.nan,dtype=np.float64)
LOCAL = []
CONF = []
for t,d in enumerate(SERIES):
    for i in range(N):
        for j in range(N):
            if d['report_age_s'][i][j] is not None:
                AGE_RAW[t,i,j] = float(d['report_age_s'][i][j])
    LOCAL.append(d['local_parents'])
    CONF.append(d['confirmed_parents'])
assert QUALITY.shape == AGE_RAW.shape == (1201,4,4)

EVENTS = {
    400:'Outage onset',
    555:'Relay requested',
    577:'AUV1 installs relay',
    587:'Local relay installed',
    597:'Acknowledgments pending',
    607:'USV confirms relay',
    645:'Local and confirmed routes differ',
    912:'Direct routes restored',
}


def timeline(anchors: list[int], segment_frames: list[int]):
    """Smooth viewing speed around anchor points; not a physical time warp."""
    assert len(segment_frames) == len(anchors)-1
    ts=[]
    for a,b,num in zip(anchors[:-1],anchors[1:],segment_frames):
        for j in range(num):
            x=j/num
            eased=x*x*(3-2*x)  # smooth arrival/departure
            ts.append(a+(b-a)*eased)
    ts.append(float(anchors[-1]))
    return np.asarray(ts)


def interpolation(t: float, source: np.ndarray, *, nan_fallback: float = 0.0):
    i = min(max(0, int(t)), len(source)-1)
    j = min(i+1,len(source)-1)
    w=np.clip(t-i,0.0,1.0)
    p=np.nan_to_num(source[i], nan=nan_fallback)
    q=np.nan_to_num(source[j], nan=nan_fallback)
    return p*(1-w)+q*w


def known_report_at(t: float):
    """A missing report is not interpolated into a newly observed report."""
    i=min(max(0,int(t)),len(AGE_RAW)-1)
    j=min(i+1,len(AGE_RAW)-1)
    # At an exact native sample, show that sample even if the next is missing.
    if abs(t-i) < 1e-8 or i == j:
        return np.isfinite(AGE_RAW[i])
    return np.isfinite(AGE_RAW[i]) & np.isfinite(AGE_RAW[j])


def age_at(t: float):
    # Zero-for-missing convention from the supplied Fig3 style.
    mat=interpolation(t,AGE_RAW)
    known=known_report_at(t)
    mat[~known]=0.0
    return mat,known


def parents_at(t: float):
    idx=min(max(0,int(t)),len(SERIES)-1)
    return LOCAL[idx],CONF[idx]


def route_matrix(parents):
    M=np.zeros((N,N),dtype=np.float64)
    if parents is not None:
        for i,p in enumerate(parents,start=1):
            if p is not None and 0<=int(p)<N and int(p)!=i:
                M[i,int(p)]=1.
    return M


def parent_label(parents):
    if parents is None:
        return '(unavailable)'
    return '  /  '.join(f'AUV{i+1}→{NAMES[int(parent)]}' if parent is not None else f'AUV{i+1}→—' for i,parent in enumerate(parents))


def event_caption(t:float):
    exact=[k for k in EVENTS if abs(t-k)<1e-7]
    if exact:
        return EVENTS[exact[0]]
    past=[k for k in EVENTS if k<=t]
    return EVENTS[max(past)] if past else 'Pre-outage'


def matrix_smooth(M, *, res=36, sigma=.9):
    big=zoom(M, zoom=res, order=3, mode='nearest', prefilter=True)
    if sigma:
        big=gaussian_filter(big,sigma=sigma,mode='nearest')
    return np.clip(big,0.,1.)


def fig_image(fig):
    # CRITICAL: no bbox_inches='tight' and no constrained/tight layout.
    # This means the pixel canvas and axes locations cannot drift.
    fig.canvas.draw()
    rgba=np.asarray(fig.canvas.buffer_rgba()).copy()
    return Image.fromarray(rgba,mode='RGBA').convert('RGB')


def quantize_shared_palette(frames: list[Image.Image]):
    # Single global palette prevents color shifts between GIF frames.
    ix=sorted(set([0,len(frames)//2,len(frames)-1]))
    ref=Image.new('RGB',(480*len(ix),270),'white')
    for j,k in enumerate(ix):
        ref.paste(frames[k].resize((480,270),Image.Resampling.BILINEAR),(480*j,0))
    palette=ref.quantize(colors=256,method=Image.Quantize.MEDIANCUT)
    return [f.quantize(palette=palette,dither=Image.Dither.NONE) for f in frames]


def save(frames, path, frame_times, key_times, normal_ms=90, key_ms=230):
    assert len(frames)==len(frame_times)
    shape=frames[0].size
    assert all(f.size==shape for f in frames), 'Jitter: inconsistent frame canvas'
    # Quantize with a common palette first to avoid color shimmering, then
    # store pixel-equivalent delta frames (disposal=1).  GIF viewers retain
    # a *fixed* logical canvas even when a delta frame has a smaller patch.
    frames=[f.convert('RGB') for f in quantize_shared_palette(frames)]
    durations=[key_ms if any(abs(float(t)-k)<.0001 for k in key_times) else normal_ms for t in frame_times]
    durations[-1]=850
    frames[0].save(path,save_all=True,append_images=frames[1:],duration=durations,
                   loop=0,disposal=1,optimize=True)
    print(f'Saved {path.name}: {len(frames)} frames, fixed {shape[0]}×{shape[1]} px, {path.stat().st_size/1e6:.1f} MB',flush=True)


def build02():
    ASSETS.mkdir(parents=True,exist_ok=True)
    anchors=[400,577,587,607,645,912]
    times=timeline(anchors,[28,15,17,19,31])
    frames=[]
    norm=colors.Normalize(0,1)
    for fi,t in enumerate(times):
        Q=interpolation(t,QUALITY)
        parents,confirmed=parents_at(t)
        B=route_matrix(parents)
        fig=plt.figure(figsize=(11.4,7.3),dpi=112)
        # All positions explicitly fixed: data cannot change axes location.
        aQ=fig.add_axes([.095,.22,.335,.59])
        aR=fig.add_axes([.555,.22,.335,.59])
        cQ=fig.add_axes([.438,.29,.012,.43])
        cR=fig.add_axes([.897,.29,.012,.43])
        plot_specs=[(aQ,cQ,Q,'magma','Received link-quality report','Destination','Reported quality',False),
                    (aR,cR,B,'viridis','Locally installed forwarding','Next hop','Installed route',True)]
        for ax,cax,M,cmap,title,xlab,clab,is_route in plot_specs:
            smooth=matrix_smooth(M)
            im=ax.imshow(smooth,origin='upper',extent=[-.5,3.5,3.5,-.5],
                         interpolation='bilinear',cmap=cmap,vmin=0,vmax=1,aspect='equal')
            ax.set_xlim(-.5,3.5);ax.set_ylim(3.5,-.5)
            ax.set_xticks(range(N));ax.set_yticks(range(N))
            ax.set_xticklabels(NAMES,fontsize=10.5)
            ax.set_yticklabels(NAMES,fontsize=10.5)
            ax.set_xlabel(xlab,fontsize=12,labelpad=10)
            ax.set_ylabel('Source',fontsize=12,labelpad=5)
            ax.set_title(title,fontsize=13,color='#23334A',pad=12)
            ax.tick_params(length=0)
            for i in range(N):
                for j in range(N):
                    txt=('—' if i==j else str(int(M[i,j]))) if is_route else ('—' if i==j else f'{M[i,j]:.2f}')
                    val=M[i,j]
                    light=val<.55
                    ax.text(j,i,txt,ha='center',va='center',
                            fontsize=10.5,color='white' if light else '#18232C',fontweight='semibold')
            for z in np.arange(-.5,N,.999999):
                ax.axhline(z,color='white',alpha=.12,lw=.5)
                ax.axvline(z,color='white',alpha=.12,lw=.5)
            cb=fig.colorbar(im,cax=cax,ticks=[0,.5,1])
            cb.ax.tick_params(labelsize=10)
            cb.set_label(clab,fontsize=10,labelpad=7)
        fig.text(.07,.939,'EXECUTION-LEVEL TOPOLOGY TRANSITION  |  3 AUVs',
                 ha='left',va='center',fontsize=18.5,color='#15324B',fontweight='bold')
        fig.text(.07,.895,f't = {t:06.2f} s',ha='left',va='center',fontsize=16.0,color='#0A8F8A')
        fig.text(.40,.895,event_caption(t),ha='left',va='center',fontsize=13.6,color='#A85B24')
        fig.text(.08,.124,'LOCAL ROUTE:      '+parent_label(parents),
                 ha='left',va='center',fontsize=11.8,color='#1D374F')
        fig.text(.08,.081,'USV CONFIRMED:  '+parent_label(confirmed),
                 ha='left',va='center',fontsize=11.8,color='#1D374F')
        fig.text(.08,.034,'Link-quality shading between 1-s records is visual interpolation; installed routes change only at recorded events.',
                 ha='left',va='center',fontsize=9.9,color='#66758A')
        # static progress bar with moving marker, not affecting layout
        x0,y0,x1=.08,.858,.90
        fig.add_artist(plt.Line2D([x0,x1],[y0,y0],transform=fig.transFigure,color='#D8E2EA',lw=2.3))
        x=x0+(t-400)/(912-400)*(x1-x0)
        fig.add_artist(plt.Line2D([x0,x],[y0,y0],transform=fig.transFigure,color='#0C938E',lw=2.8))
        frames.append(fig_image(fig));plt.close(fig)
        if fi%15==0:
            print('02 frame',fi+1,'/',len(times),'at',f'{t:.2f}','s',flush=True)
    save(frames,ASSETS/'02_topology_transition_3auv.gif',times,anchors,normal_ms=86,key_ms=220)
    return frames,times


def smooth_surface(M,n=62):
    coords=np.arange(N,dtype=float)
    fine=np.linspace(0,N-1,n)
    z1=PchipInterpolator(coords,M.T,axis=1)(fine)
    z2=PchipInterpolator(coords,z1,axis=0)(fine)
    z2=gaussian_filter(z2,sigma=.65)
    z2=np.clip(z2,0.,125.)
    X,Y=np.meshgrid(fine,fine)
    return X,Y,z2


def build03():
    anchors=[400,555,577,587,597,607,645,912]
    times=timeline(anchors,[24,11,9,9,9,13,32])
    frames=[]
    norm=colors.Normalize(vmin=0.,vmax=125.)
    for fi,t in enumerate(times):
        M,known=age_at(t)
        X,Y,Z=smooth_surface(M)
        fig=plt.figure(figsize=(11.6,7.45),dpi=110)
        ax=fig.add_axes([.012,.145,.585,.70],projection='3d')
        cx=fig.add_axes([.608,.26,.012,.46])
        # fixed 3D axes limits/view/box -- never autoscale per frame
        ax.plot_surface(X,Y,Z,cmap='viridis',norm=norm,rcount=40,ccount=40,
                        linewidth=0,antialiased=True,shade=False,alpha=.97)
        ax.contourf(X,Y,Z,zdir='z',offset=0.,levels=np.linspace(0,125,15),
                    cmap='viridis',norm=norm,alpha=.45)
        si,ri=np.where(known & (~np.eye(N,dtype=bool)))
        if len(si):
            ax.scatter(si,ri,np.minimum(125,M[si,ri]),s=18,c='white',
                       edgecolors='#173249',linewidth=.6,depthshade=False)
        ax.set(xlim=(0,3),ylim=(0,3),zlim=(0,125))
        ax.set_xticks([0,1,2,3]);ax.set_yticks([0,1,2,3]);ax.set_zticks([0,60,125])
        ax.set_xticklabels(list(NAMES[:-1])+[''],fontsize=10.3)
        ax.set_yticklabels(['' if name in ('USV','AUV1') else name for name in NAMES],fontsize=10.3)
        ax.set_zticklabels(['0','60','125'],fontsize=9.5)
        ax.set_xlabel('Source',labelpad=7,fontsize=11)
        ax.set_ylabel('Receiver',labelpad=8,fontsize=11)
        ax.tick_params(axis='x',pad=-1)
        ax.tick_params(axis='y',pad=0)
        ax.view_init(elev=33,azim=-62)
        ax.set_box_aspect((1.1,1.05,.70),zoom=.91)
        for axis in [ax.xaxis,ax.yaxis,ax.zaxis]:
            axis.pane.fill=True
            axis.pane.set_facecolor((.97,.98,1.0,.72))
            axis.pane.set_edgecolor((.82,.87,.92,1.0))
            axis._axinfo['grid']['color']=(.70,.75,.81,.23)
        fig.colorbar(cm.ScalarMappable(norm=norm,cmap='viridis'),cax=cx,ticks=[0,30,60,90,125])
        cx.tick_params(labelsize=9)
        cx.set_ylabel('Age (s)',fontsize=10,labelpad=5)
        # Disambiguate the corner labels without changing their true 3D tick anchors.
        def corner_label(text,xx,yy,offset,align='center'):
            xx2,yy2,_ = proj3d.proj_transform(xx,yy,0,ax.get_proj())
            ax.annotate(text,xy=(xx2,yy2),xycoords=ax.transData,
                        xytext=offset,textcoords='offset points',
                        fontsize=10.0,color='#273C4D',ha=align,va='center',annotation_clip=False)
        corner_label(NAMES[-1],3,0,(-25,-17),'right')
        corner_label('USV',3,0,(22,-17),'left')
        corner_label('AUV1',3,1,(32,-10),'left')
        aM=fig.add_axes([.705,.355,.285,.405])
        aM.imshow(np.minimum(125,M),origin='upper',cmap='viridis',vmin=0,vmax=125,interpolation='nearest')
        aM.set_xticks(range(4));aM.set_yticks(range(4))
        aM.set_xticklabels(NAMES,fontsize=9.5)
        aM.set_yticklabels(NAMES,fontsize=9.5)
        aM.set_xlabel('Receiver',fontsize=10)
        aM.set_ylabel('Source',fontsize=10)
        aM.tick_params(length=0)
        for i in range(N):
            for j in range(N):
                txt=f'{M[i,j]:5.1f}' if known[i,j] and i!=j else '  —'
                aM.text(j,i,txt,ha='center',va='center',fontfamily='monospace',
                        fontsize=9.2,color=('#132B43' if M[i,j]>78 else 'white'),fontweight='bold')
        fig.text(.700,.824,'REPORTED VALUES (s)',fontsize=12.6,fontweight='bold',color='#22374E')
        fig.text(.700,.793,'Ordered source → receiver pairs',fontsize=9.4,color='#5E6E7E')
        fig.text(.700,.264,'—  missing / self-link',fontsize=9.6,color='#647485')
        fig.text(.075,.94,'RECEIVER-HELD LINK-REPORT AGE  |  3 AUVs',
                 fontsize=19,color='#17324A',fontweight='bold',va='center')
        fig.text(.075,.887,f't = {t:06.2f} s',fontsize=16.1,color='#0C968C',va='center')
        fig.text(.343,.887,event_caption(t),fontsize=13.4,color='#A65A21',va='center')
        # progress line fixed in all frames
        x0,y0,x1=.075,.854,.93
        fig.add_artist(plt.Line2D([x0,x1],[y0,y0],transform=fig.transFigure,color='#D8E2EA',lw=2.3))
        xpos=x0+(t-400)/(912-400)*(x1-x0)
        fig.add_artist(plt.Line2D([x0,xpos],[y0,y0],transform=fig.transFigure,color='#0A948D',lw=2.8))
        fig.text(.075,.072,'Original 1-s replay matrices; fractional display times use adjacent-sample interpolation.',
                 fontsize=10.1,color='#566B7B')
        fig.text(.075,.042,'Z/color scale fixed at 0–125 s. Missing/self-link cells shown at zero only as a plotting convention.',
                 fontsize=9.3,color='#637486')
        frames.append(fig_image(fig));plt.close(fig)
        if fi%15==0:
            print('03 frame',fi+1,'/',len(times),'at',f'{t:.2f}','s',flush=True)
    save(frames,ASSETS/'03_link_age_3d.gif',times,anchors,normal_ms=90,key_ms=250)
    return frames,times


if __name__=='__main__':
    a,at=build02()
    b,bt=build03()
    # visual sanity contact sheet from a few key frames
    for fn,frames in [('02',a),('03',b)]:
        indices=[0,len(frames)//3,2*len(frames)//3,len(frames)-1]
        w,h=frames[0].size
        thumbw=560
        thumbh=round(h*thumbw/w)
        sheet=Image.new('RGB',(thumbw*2,thumbh*2),'white')
        for k,idx in enumerate(indices):
            sheet.paste(frames[idx].resize((thumbw,thumbh),Image.Resampling.LANCZOS),((k%2)*thumbw,(k//2)*thumbh))
        path=ASSETS/f'{fn}_contact_sheet.png'
        sheet.save(path,optimize=True)
        print('Preview',path,flush=True)
