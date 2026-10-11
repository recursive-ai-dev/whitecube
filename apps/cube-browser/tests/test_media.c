#include "media.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

int main(void){
    struct cb_media_env e={800,600,0,0};
    const char *matches[]={NULL,"","  ","all","screen","SCREEN","only screen",
        "not print","not unknown","(width:800px)","(min-width:800px)","(max-width:800px)",
        "screen and (min-width:50em) and (max-height:37.5rem)","(min-height:6e2px)",
        "(orientation:landscape)","(width)","(height)","(orientation)",
        "(prefers-reduced-motion:no-preference)","(prefers-contrast:no-preference)",
        "print, (min-width:700px)","not screen and (max-width:700px)",
        "not (max-width:700px)","((max-width:700px) or (width:800px))",
        "((min-width:700px) and (max-width:900px)) and (height:600px)",
        "(made-up-feature) or (width:800px)","not ((made-up-feature) and (width:1px))",
        "screen/**/and/**/(width:800px)","garbage!, screen","(max-width:-1px),screen"};
    for(size_t i=0;i<sizeof matches/sizeof matches[0];i++){
        if(!cb_media_matches(matches[i],&e)){fprintf(stderr,"Expected match: %s\n",matches[i]);return 1;}
    }
    const char *misses[]={"print","speech","not screen","not all","only print",
        "(max-width:799.99px)","(min-height:601px)","(width:800)","(width:80 0px)",
        "(width:0x320px)","(width:nan)","(width:1e999px)","(width:100%)","(min-width:1.px)",
        "(orientation:portrait)","(prefers-reduced-motion:reduce)","(prefers-contrast:more)",
        "(min-orientation:landscape)","(made-up-feature)","not (made-up-feature)",
        "not screen and (made-up-feature)","screen and (width:800px) or (height:600px)",
        "(width:800px) and (height:600px) or (orientation:landscape)",
        "not(width:1px)","screen and(width:800px)","not not screen","only (width:800px)",
        "screen and","screen (width:800px)","screen and ()","(width:800px", 
        "(width:800px))","screen/*unfinished","(width:800px) trailing","and","or","only",","};
    for(size_t i=0;i<sizeof misses/sizeof misses[0];i++){
        if(cb_media_matches(misses[i],&e)){fprintf(stderr,"Unexpected match: %s\n",misses[i]);return 1;}
    }
    e.width=600;e.height=600;e.reduced_motion=e.strong_contrast=1;
    assert(cb_media_matches("(orientation:portrait)",&e));
    assert(cb_media_matches("(prefers-reduced-motion) and (prefers-contrast:more)",&e));
    assert(!cb_media_matches("(prefers-reduced-motion:no-preference)",&e));
    assert(!cb_media_matches("(prefers-contrast:less)",&e));
    assert(cb_media_matches("(min-width:-1px)",&e));
    assert(!cb_media_matches("(max-width:-1px)",&e));
    e.width=0;assert(cb_media_matches("(width:0)",&e));assert(!cb_media_matches("(width)",&e));
    e.width=NAN;assert(!cb_media_matches("all",&e));assert(!cb_media_matches("all",NULL));
    e.width=800;
    char huge[4096];memset(huge,' ',sizeof huge);memcpy(huge,"screen",6);huge[sizeof huge-1]=0;
    assert(!cb_media_matches(huge,&e));
    char nested[128]={0};memset(nested,'(',17);strcat(nested,"width:800px");
    for(unsigned i=0;i<17;i++)strcat(nested,")");
    assert(!cb_media_matches(nested,&e));
    /* Exercise every truncation of a valid query: all must be bounded and safe. */
    const char *sample="screen and ((min-width:40em) or (orientation:portrait))";
    for(size_t n=0;n<=strlen(sample);n++){char prefix[128];memcpy(prefix,sample,n);prefix[n]=0;cb_media_matches(prefix,&e);}
    puts("media: viewport, preferences, boolean logic, lists, invalid input and bounds passed");
    return 0;
}
