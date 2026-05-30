GENERATE_PROMPT = """ 
You need to use this image as a reference and make a dxf file adhering to SBC (Seattle Building Code)
https://www.seattle.gov/sdci/codes/codes-we-enforce-(a-z)/building-code
I have linked the relevant documentation below,
Also ensure that plot area is maximum and the plot is in a layer called SBC_HOUSE_FOOTPRINT
To generate the file you need to give me code that uses ezdzf and shapely and offsets from the SBC to generate
the required dxf file.
"""

