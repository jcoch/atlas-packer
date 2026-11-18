Texture Atlas Compiler (pack.py)

This is a utility that will create a batch of texture atlases from a batch of
configuration files. This utility creates tightly packed texture atlases, and
when creating a large number of related atlases, it does this job quite rapidly
by sharing labor between the atlases, reducing the total amount of work. The
larger the set of atlases it has to process, the more effort per atlas that
it's capable of reducing. This makes it both efficient with packing density
and rapid when handling multiple atlases.

Requirements:
   Python 3.8+
   Pillow (PIL) with WebP support
      pip install Pillow

Usage:

   python3 pack.py <command line options>...

The above command will invoke pack.py and cause it to search the directory
tree rooted at the location of pack.py itself. Every file within that tree
that matches the glob "wtxpck.*.conf" will be considered a configuration
file for a single atlas. You currently cannot specify another location to
search for configuration files.

Command Options:
  --parallel
  --sequential
    These two options enable/disable multi-threaded creation of atlases.

  --pretty
  --compact
    These two options dictate the format of the json manifest file(s)
    associated with each atlas page. "--pretty" causes the json files
    to be multiple indented lines that are easily read by a human.
    "--compact" causes the json files to be compacted onto a single 
    line, with all whitespace removed to reduce file size.

  --share
  --noshare
    These two options enable/disable labor and image sharing between
    atlases being constructed or modified. Allowing sharing enables,
    under ideal conditions, pack.py to build a set of atlases in
    less than half the time it would take without sharing.

  --force
    This option tells pack.py that it needs to ignore any existing
    timestamps on the source image files, configuration files,
    and the atlases themselves. It will force a complete rebuild
    of the atlases from scratch.

The default values for the above options are defined by a series of
assignments to variables named DEFAULT_<parameter> that are located
near the top of the script. The intent is to make it easy for the user
of the script to tailor the default behavior to their own preferences.

Configuration file format:

The configuration file is simply a series of semicolon-terminated lines
of the format:

   name = value ;

Where value is either a number or a string. Leading and trailing spaces are
ignored, while spaces in the middle of the value are considered an error.

For instance, the lines

prefix=Super_Game;       Perfectly acceptable.
 prefix = Super_Game ;   Also acceptable.
prefix=Super Game;       Is considered an error.

If a configuration file contains any errors, it will be ignored and skipped
during the batch build of the atlases.

Unknown parameter names are silently ignored.

Parameter names and meanings:
   input
             Example: input=../Game/;
             Status:  Mandatory
             Default: None
             Description:
                This parameter defines the directory location where the image
             files will be found. It is a standard directory path name. If it
             is a relative path name, it will be relative to the location of
             the configuration file itself.

   output
             Example: output=../Compiled/Atlases/;
             Status:  Mandatory
             Default: None
             Description:
                This parameter defines the directory in which the created
             atlas will be stored. It is a standard directory path name. If
             it is a relative path name, it will be relative to the location
             of the configuration file itself.

   prefix
             Example: prefix=Heroes;
             Status:  Optional, but extremely recommended.
             Default: ""
             Description:
                This is a prefix that is prepended to the partial filename
             for each input image. Conceptually, it can be considered to be
             a "virtual pathname" tying all image assets to the location
             of some asset root. Because of this, its value is usually some
             portion of the tail end of the input directory value.
             For example:
                The following parameters have been set.
                    input=../../GameAssets;
                    prefix=SuperGame

                and while searching for images, the following file was found
                with the full filename of:
                    ../../GameAssets/Clothing/Helmet.png

                The internal asset name for the above image in the json
                manifest would be:
                    SuperGame/Clothing/Helmet.png

                With the above example input and prefix values, in a real game,
                the value of prefix for that specific input directory would
                either be left blank (all game image assets live under/inside
                the "../../GameAssets" directory), or it would typically have
                the value of "GameAssets" (there are multiple directories at
                the same level as GameAssets that also hold images used by
                other atlases the game requires). This hypothetical example
                violates those recommendations solely to demonstrate that the
                recommendation is not mandatory.

   atlas_prefix
             Example: atlas_prefix=game_;
             Status:  Mandatory
             Default: None
             Description:
                This value is the base filename used for naming the atlas
             page files. Each generated atlas page will use this prefix
             and concatenate a 4-digit number to it in order to create
             a base page name. This base page name in turn is used in naming
             two files with the extensions of "json" and "webp" to hold
             the atlas manifest inside the page, and the page image itself.
             For example, assume atlas_prefix is "Super", and that the
             atlas takes two pages. This will cause the atlas to consist of
             four files, named: Super0000.json, Super0000.webp,
                 Super0001.json, Super0001.webp

   atlas_size
             Example: atlas_size=4096;
             Status:  Optional
             Default: 2048
             Description:
                This determines the maximum allowed edge length of the
             generated atlas images. It must be an exact power of 2.
             If a non-power of 2 is specified, a warning will be issued
             and the value rounded up to the next larger exact power of 2.
             As an example, if 2049 is requested, it will be treated as if
             the value 4096 was actually requested.

   atlas_min
             Example: atlas_min=32;
             Status:  Optional
             Default: 64
             Description:
                This determines the minimum allowed edge length of the
             generated atlas images. As with atlas_size, it must be an
             exact power of 2. It will be rounded up and a warning issued
             if a non-power of 2 is specified.

   input_scale
             Example: input_scale=1.0;
             Status:  Optional
             Default: 1.0
             Description:
                This is simply a scaling factor to resize a raw input image
             before incorporating the image into an atlas. 1.0 for full size,
             0.5 for half size, 0.25 for quarter size, etc.

   alpha_cutoff
             Example: alpha_cutoff=0;
             Status:  Optional
             Default: 0
             Description:
                This parameter determines the alpha value of an RGBA pixel which
             is considered "invisible" and can therefore be clipped from the
             stored image. An alpha value of 0 is a totally transparent pixel.
             However, some image utilities will leave behind extremely faint
             pixels (alpha values of 1 or 2) that prevent clipping of an image
             because the pixels are not quite invisible. This parameter is
             intended to deal with that problem.

   padding
             Example: padding=1;
             Status:  Optional
             Default: 1
             Description:
                This defines how wide of a border of transparent pixels to
             leave between images in the atlas.

THEORY OF OPERATION

pack.py gets much of its speed by exploiting three central concepts. These
concepts allow it to reduce the total amount of work required and therefore
reduce the total amount of time doing that work. These concepts are:

   1. Self-caching. An atlas can act as a cache for itself when images are
      added or modified for a pre-existing atlas. For instance, if you have
      an atlas with 6000 images in it and you need to add a few more images
      later, say 3 images, pack.py will perform all the image processing
      it needs to do on just those 3 images. It will then construct the
      desired 6003-image atlas by using those 3 images plus the already
      available images in the original atlas itself.

   2. Work sharing. There may be multiple atlases that share the exact
      same set of input images, but use different scaling factors (say one
      scale is for use when the game is running on a PC, while a lower
      resolution and smaller scale is used for the game when running on a
      tablet or smart phone). Because these atlases use the same source
      images, pack.py can perform the alpha clipping of the original image
      once, and then each individual atlas just needs to scale the
      pre-clipped image for itself, saving a lot of redundant image
      processing. I call these sets of atlases "siblings". Two atlases are
      considered siblings of each other if they share the same set of input
      images and have the same alpha_cutoff values.

   3. Cross-atlas caching. If two atlases use the same input_scale and
      alpha_cutoff values, then the images within them have to undergo
      the same image processing. If, additionally, one of those atlases
      contains a subset of the images used in the other atlas, then it's
      possible for either of those atlases to use the other atlas as
      a cache supplying already-processed images, ready for direct
      inclusion. I call this a parent/child relationship. For my purposes,
      the child atlas has a proper subset of the images in the parent atlas.
      Additionally, the child acts as a source cache for the parent.
      Although, in theory, the parent can act as a cache for the child,
      pack.py does not do this.

Now, for the ordering of when pack.py actually processes each atlas.
Given the above descriptions, the following happens:

1. Parent/child/neither. The atlases are categorized as being parents that
   can draw upon a child for cached images, and those atlases that fall
   into neither category.

2. After the parents are identified, the remaining atlases are arranged
   into sibling groups. A sibling group contains all the atlases that have
   matching input values (after normalization) and alpha_cutoff values.

After the above processing is done, pack.py has a collection of jobs to
perform. Each job will process one or more atlases. Additionally, each
job is flagged as a "child" (one or more atlases within the job can
act as a cache source), a "parent" (the atlas in this job can use a child
as a cache source), or a "loner" (none of the atlases in this job
partake in cache sharing).

Finally, the collection of jobs is executed in order.
1. All of the child jobs are performed first.
2. None of the parent jobs are allowed to start until after the last
   child job has finished.
3. The loner jobs are allowed to use any idle worker thread if one is
   available and there are still child jobs executing. If there are
   no children executing, they get processed after the parent jobs
   finish.

Warning messages:

There are two warning messages that pertain to optimization.

1. Warning: Atlases <name> and <name> are cousins, not siblings.

   I consider two atlases that have matching sets of input images, but
   differing alpha_cutoff values, to be cousins of each other. They are
   NOT eligible for work sharing due to the different alpha_cutoff values.
   Because of how the alpha_cutoff value is used, it's extremely likely
   that the alpha_cutoff difference is due to a typo or oversight. If
   those alpha_cutoff values are made to match, then on the next run
   of pack.py, those atlases would be considered siblings of each other
   and become able to share in the initial image processing.

2. Warning: Atlas <name> has a missing child.
   This warning happens when an atlas has been evaluated as being a parent
   to one or more children, but still has images that are not cached by
   a child. That partial cache coverage can be caused by a multitude of
   reasons, so I can't tell you exactly why the coverage is missing.
   Some common causes:
      A. The child atlas really doesn't exist.
      B. The intended child atlas does exist, but there's a difference in
         either the input_scale values or the alpha_cutoff values.

Performance recommendations:

1. Use consistent alpha_cutoff values! A different alpha_cutoff between two
atlases that could have shared image processing or cache use disables that
optimization due to those different values. The single smallest change that
can have a huge impact on execution time is to simply use the same
alpha_cutoff value for all of your atlases.

2. If you have a large atlas that contains thousands of images, make sure it's
a parent and has lots of small children providing total cache coverage. I
cannot emphasize this point strongly enough. Having a few hundred children does
increase the total CPU processing by a small amount. But, if your computer has
a large number of cores (4, 8, 16, or more), then this parent/child relationship
allows you to use those cores to reduce the wall clock time required to
generate your atlas. The reason is because each atlas, by its very nature, runs
on a single thread, and a large atlas requires a long time to process from
beginning to end. However, the child atlases can all run in parallel with each
other, using all those cores the computer has available. Hence, the wall clock
time for a few hundred children being built is far smaller. And then when the
parent atlas is built, it does not have to do ANY image processing. It just
needs to collect the already-processed images from its children, then pack
and save the atlas.
